"""Temporal workflow for Board 10 — pre-alert shipment email processing agent.

Generated from spec: Board 10
Frozen at: 2026-06-29T07:03:56.817564+00:00
DO NOT EDIT MANUALLY — regenerate via POST /api/v1/boards/1d44ee9c-a956-41fe-bc9e-6a05b20196d5/codegen
"""
import asyncio
import re as _re
from dataclasses import dataclass, field
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from backend.runtime.activities.classify_documents import classify_documents
    from backend.runtime.activities.extract_email_facts import extract_email_facts
    from backend.runtime.activities.fetch_email import fetch_email_and_attachments
    from backend.runtime.activities.load_document import load_document
    from backend.runtime.activities.validate_fields import validate_required_fields
    from backend.runtime.activities.match_documents import match_by_key
    from backend.runtime.activities.tally import tally
    from backend.runtime.activities.report import emit_report, send_report
    from backend.runtime.activities.email_dedup import is_email_processed, mark_email_processed
    from backend.runtime.activities._types import (
        ClassifyDocumentsInput,
        ExtractEmailFactsInput,
        FetchEmailInput, LoadDocumentInput, ValidateFieldsInput,
        MatchInput, TallyInput, EmitReportInput, SendReportInput,
        EmailDedupInput,
    )

_TIMEOUT = timedelta(seconds=60)
_VISION_TIMEOUT = timedelta(seconds=180)   # load_document calls that may invoke Claude vision
_CLASSIFY_TIMEOUT = timedelta(minutes=10)  # classify_documents: parallel vision across all attachments

# ---------------------------------------------------------------------------
# Regex helpers (P2 pattern) — extract batch numbers from invoice text
# ---------------------------------------------------------------------------
# Batch number tokens: mixed alphanumeric, 5+ chars (e.g. "AB1234", "C04B001")
_BATCH_TOKEN_RE = _re.compile(r'\b([A-Za-z0-9]{5,})\b')


def _extract_batch_numbers(doc_text: str) -> list[str]:
    """Extract deduplicated batch numbers from an invoice's BATCH NOS: field.

    Applies P2 pattern: scan the text after 'BATCH NOS:' label AND the next
    line, filter for mixed alphanumeric tokens (reject pure-alpha words and
    pure-numeric strings).  Collects ALL occurrences across the document so
    that multi-page invoices with repeated BATCH NOS: sections are handled.
    Returns a deduplicated list preserving first-seen order.
    """
    lines = doc_text.splitlines()
    seen: dict[str, int] = {}   # token → first-seen index (preserves order)
    order: list[str] = []

    for i, line in enumerate(lines):
        upper = line.upper()
        if "BATCH NOS" not in upper:
            continue
        # Extract text after the colon on this line
        colon_idx = line.find(":")
        after = line[colon_idx + 1:] if colon_idx != -1 else ""
        # Also grab the next line (P2 layout (c))
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        search_text = after + " " + next_line
        for m in _BATCH_TOKEN_RE.finditer(search_text):
            tok = m.group(1)
            # Must be mixed alphanumeric (contains at least one letter and one digit)
            if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
                if tok not in seen:
                    seen[tok] = len(order)
                    order.append(tok)

    return order


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Board10AgentInput:
    message_id: str = ""
    use_fixture: bool = True
    fixture_body: str = ""
    fixture_attachments: list[dict] = field(default_factory=list)
    board_id: str = ""
    fixture_subject: str = ""   # eval/fixture runs set the shipment key via this field
    fixture_sender: str = ""


@dataclass
class Board10AgentResult:
    message_id: str
    shipment_number: str = ""
    invoices_processed: int = 0
    invoices_succeeded: int = 0
    invoices_failed: int = 0
    goods_failed: int = 0
    batches_processed: int = 0
    batches_succeeded: int = 0
    batches_failed: int = 0
    report_content: str = ""
    already_processed: bool = False


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@workflow.defn
class Board10AgentWorkflow:
    @workflow.run
    async def run(self, inp: Board10AgentInput) -> Board10AgentResult:

        # ── Dedup guard ────────────────────────────────────────────────────
        dedup = await workflow.execute_activity(
            is_email_processed,
            EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
            start_to_close_timeout=_TIMEOUT,
        )
        if dedup.already_processed:
            return Board10AgentResult(
                message_id=inp.message_id,
                already_processed=True,
            )

        # ── Step 1: Fetch email + attachments ──────────────────────────────
        # (tool_action fetch node: "Read / Fetch data")
        fetch_result = await workflow.execute_activity(
            fetch_email_and_attachments,
            FetchEmailInput(
                message_id=inp.message_id,
                use_fixture=inp.use_fixture,
                fixture_subject=inp.fixture_subject,
                fixture_sender=inp.fixture_sender,
                fixture_body=inp.fixture_body,
                fixture_attachments=inp.fixture_attachments,
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # ── Step 2: Extract email facts (P4) ──────────────────────────────
        # LLM extracts the shipment identifier (MAWB for air / container for sea)
        # and the invoice number list from the subject/body.
        # Shipment ID: MAWB format NNN-NNNNNNNN (air) or container number (sea).
        # Invoice hint: invoice numbers appear after 'INV NO'/'INVOICE NO' as
        # comma-separated codes like U03/26-27/382; trailing number is the invoice number.
        email_facts = await workflow.execute_activity(
            extract_email_facts,
            ExtractEmailFactsInput(
                subject=fetch_result.subject,
                body=fetch_result.body_text,
                shipment_id_hint=(
                    "MAWB number (format NNN-NNNNNNNN) for air shipments, "
                    "or container number for sea shipments. "
                    "Extract from subject or body only — do NOT fall back to invoice content."
                ),
                invoice_hint=(
                    "Invoice numbers appear after 'INV NO' or 'INVOICE NO' as a "
                    "comma-separated list in the form <CODE>/<period>/<number> "
                    "(e.g. U03/26-27/382). The trailing numeric portion (e.g. '382') "
                    "is the invoice number. Expand abbreviated forms."
                ),
            ),
            start_to_close_timeout=_CLASSIFY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        shipment_number = email_facts.shipment_id or fetch_result.subject.strip()
        invoice_numbers = email_facts.invoice_numbers  # e.g. ["382", "385"]

        # ── Step 3: Classify all attachments by content (P3) ──────────────
        # classify_documents identifies doc types by CONTENT, not filename.
        # Commercial invoices must contain regulatory validation codes
        # (HTS No, ANDA No, FDA No, Reg.No, NDC No) — these are absent from packing lists.
        # COAs are identified by the 'Alias Batch No.' label in their text.
        classify_result = await workflow.execute_activity(
            classify_documents,
            ClassifyDocumentsInput(
                attachments=fetch_result.attachments,
                doc_types=[
                    {
                        "type_name": "commercial_invoice",
                        "description": (
                            "Commercial invoice for pharmaceutical goods. "
                            "Text MUST contain at least one of: 'HTS No', 'ANDA No', 'FDA No', "
                            "'Reg.No', or 'NDC No'. These regulatory/validation codes are the "
                            "decisive distinguishing markers — they are absent from packing lists "
                            "covering the same shipment. Also typically contains 'BATCH NOS:' "
                            "listing batch numbers for each line item. "
                            "Do NOT classify as commercial_invoice if the text lacks all five codes."
                        ),
                    },
                    {
                        "type_name": "certificate_of_analysis",
                        "description": (
                            "Certificate of Analysis (COA) for a pharmaceutical batch. "
                            "Text MUST contain 'Alias Batch No.' (the batch identifier label) "
                            "or clearly shows analysis results / specification values for a batch. "
                            "COAs are often scanned PDFs; use vision extraction to read them. "
                            "The filename typically contains a batch number but may also include "
                            "'COA' or other suffixes — classify by content, not filename."
                        ),
                    },
                    {
                        "type_name": "packing_list",
                        "description": (
                            "Packing list or carton list for the shipment. Shows item counts, "
                            "quantities, carton numbers, and weights for the same goods but does "
                            "NOT contain regulatory validation codes (HTS No, ANDA No, FDA No, "
                            "Reg.No, NDC No). Filename often ends with '-PL.pdf' or ' PL.pdf' "
                            "but classify by content absence of those codes."
                        ),
                    },
                    {
                        "type_name": "other",
                        "description": (
                            "Logistics, transport, Bill of Lading, airway bill, or any "
                            "uncategorised document that does not fit the above types."
                        ),
                    },
                ],
            ),
            start_to_close_timeout=_CLASSIFY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

        # Select document sets by classified type
        invoice_docs = [d for d in classify_result.documents if d["doc_type"] == "commercial_invoice"]
        coa_docs = [d for d in classify_result.documents if d["doc_type"] == "certificate_of_analysis"]

        # ── Step 4: Reconcile invoice_numbers with classify_documents (P4) ─
        # Match by content: (a) classifier identifier, (b) doc text, (c) filename.
        # Do NOT match by filename-ending alone — prefixes vary per sender.
        # Resolved assumption: invoices_in_shipment = collection from "Read / Fetch data"
        # that the outer scope ("Repeat for each Invoice") iterates.
        invoices_in_shipment: list[dict] = []
        if invoice_numbers:
            remaining = list(invoice_docs)
            for inv_num in invoice_numbers:
                def _content_match(d: dict, _n: str = inv_num) -> bool:
                    ident = d.get("identifier", "")
                    text = d.get("text", "")
                    name = d.get("name", "")
                    # (a) classifier-extracted identifier contains the invoice number
                    if _n in ident:
                        return True
                    # (b) full document text contains the invoice number
                    if _n in text:
                        return True
                    # (c) last resort: filename contains the invoice number
                    if _n in name:
                        return True
                    return False
                matched = next((d for d in remaining if _content_match(d)), None)
                if matched:
                    invoices_in_shipment.append(matched)
                    remaining.remove(matched)
                # Do NOT append empty stubs — they produce false validation failures
            if not invoices_in_shipment:
                # None matched by content — fall back to all classified commercial invoices
                invoices_in_shipment = invoice_docs
        else:
            # No invoice list in email — trust strict content classification
            invoices_in_shipment = invoice_docs

        # Build COA lookup by identifier (batch number extracted by classifier)
        coa_by_batch: dict[str, dict] = {
            coa["identifier"]: coa
            for coa in coa_docs
            if coa.get("identifier")
        }

        # ── Step 5: Outer loop — "Repeat for each Invoice" ────────────────
        # Resolved assumption: iterate over invoices_in_shipment (the exact variable
        # named in the blocking assumption answer).

        # Accumulators for tally
        all_tally_results: list[dict] = []

        # Shipment-level counters (aggregated for the single CSV row)
        invoices_processed = 0
        invoices_succeeded = 0
        invoices_failed = 0
        goods_failed_total = 0       # distinct failed line items across all invoices
        batches_processed_total = 0
        batches_succeeded_total = 0
        batches_failed_total = 0

        for inv in invoices_in_shipment:
            invoices_processed += 1
            inv_text = inv.get("text", "")
            inv_name = inv.get("name", "")

            # ── Step 5a: Validate the invoice (extract_validate node) ──────
            # "Check a document" — validate HTS, ANDA, FDA, REG NO, NDC, BATCH NOS
            # per_document scope; fail_if="any_missing".
            # P1: normalise whitespace for field matching (collapses \n, \t → single space)
            normalised_inv_text = " ".join(inv_text.split())

            inv_validation = await workflow.execute_activity(
                validate_required_fields,
                ValidateFieldsInput(
                    document_text=normalised_inv_text,
                    fields=[
                        # Required regulatory/validation codes per resolved assumptions.
                        # Only these five are validated (DUNS intentionally excluded).
                        {"name": "HTS No :", "appears_as": "HTS No", "scope": "document", "required": True},
                        {"name": "ANDA No :", "appears_as": "ANDA No :", "scope": "document", "required": True},
                        {"name": "FDA No :", "appears_as": "FDA No :", "scope": "document", "required": True},
                        # REG NO: whitespace/newline tolerant, tolerate FEI prefix
                        {"name": "Reg.No", "appears_as": "Reg.No", "scope": "document", "required": True},
                        {"name": "NDC No :", "appears_as": "NDC No :", "scope": "document", "required": True},
                        # BATCH NOS: collected field (not a pass/fail validation field per resolved assumption)
                        {"name": "BATCH NOS:", "appears_as": "BATCH NOS:", "scope": "document", "required": False},
                    ],
                    fail_if="any_missing",
                    applies_to="per_document",
                ),
                start_to_close_timeout=_TIMEOUT,
            )

            # Determine per-invoice pass/fail (only the five regulatory fields matter)
            # A "line item" (good) is FAILED if any of the five required fields is missing.
            # Per resolved assumption: goods_failed counts distinct failed line items.
            # In this invoice-level validation model, the entire invoice is 1 "line item" check.
            inv_passed = inv_validation.passed
            if inv_passed:
                invoices_succeeded += 1
            else:
                invoices_failed += 1
                goods_failed_total += 1   # this invoice's goods are flagged as failed

            # ── Step 5b: Extract batch numbers from invoice (P2) ──────────
            # Resolved assumption: aggregate and deduplicate batch numbers across
            # all BATCH NOS: occurrences in this invoice (multi-page).
            # P2 pattern applied to original (non-normalised) text for line-based parsing.
            batch_numbers = _extract_batch_numbers(inv_text)

            # Record this invoice's tally entry
            all_tally_results.append({
                "invoice_name": inv_name,
                "passed": inv_passed,
                "invoice_processed": True,
            })

            # ── Step 5c: Inner loop — "Repeat for each batch on the invoice" ─
            # Resolved assumption: iterate deduplicated batch numbers from this invoice.

            for batch_no in batch_numbers:
                batches_processed_total += 1

                # ── Match batch → COA (match_documents node) ──────────────
                # Left side: current batch_no from inner loop.
                # Right side: COA document carrying that batch number (Alias Batch No.).
                # P3: classify_documents already extracted COA identifiers (Alias Batch No.)
                # P5: separator-tolerant + literal fallback for COA key extraction.

                # Look up COA by classifier-extracted identifier
                coa = coa_by_batch.get(batch_no) or coa_by_batch.get(batch_no.upper())

                if coa is None:
                    # P5 fallback: scan COA docs whose LLM identifier was empty,
                    # check if batch_no appears literally in the COA text or filename
                    for c in coa_docs:
                        if not c.get("identifier"):
                            coa_text = c.get("text", "")
                            coa_name = c.get("name", "")
                            if batch_no in coa_text or batch_no in coa_name:
                                coa = c
                                break

                if coa is None:
                    # Also try: COA identifier is a different casing/suffix variant
                    # Try partial match: any COA identifier that CONTAINS the batch_no
                    for ident_key, coa_candidate in coa_by_batch.items():
                        if batch_no in ident_key or ident_key in batch_no:
                            coa = coa_candidate
                            break

                # Build source and target items for match_by_key
                source_items = [{"batch_no": batch_no, "invoice": inv_name}]

                if coa is not None:
                    # Extract the Alias Batch No. from COA text (P5 pattern)
                    _ALIAS_RE = _re.compile(
                        r'Alias\s+Batch\s+No\.?'   # label with optional trailing period
                        r'[\s|:.]*'                 # any separators (pipe, colon, dot, whitespace)
                        r'([A-Za-z0-9]{4,})',       # batch number token
                        _re.IGNORECASE,
                    )
                    coa_text = coa.get("text", "")
                    _m = _ALIAS_RE.search(coa_text)
                    alias_batch = _m.group(1).strip() if _m else ""

                    # P5 literal fallback: if regex yielded empty but batch_no is in COA text
                    if not alias_batch and batch_no and batch_no in coa_text:
                        alias_batch = batch_no

                    if not alias_batch:
                        # Use classifier identifier as last resort
                        alias_batch = coa.get("identifier", "")

                    target_items = [{"batch_no": alias_batch or batch_no, "coa_name": coa.get("name", "")}]
                else:
                    # No COA found for this batch number — match will fail
                    target_items = []

                # Call match_by_key
                match_result = await workflow.execute_activity(
                    match_by_key,
                    MatchInput(
                        source_items=source_items,
                        target_items=target_items,
                        key_field="batch_no",
                        on_missing="fail",
                        match_type="exact",
                    ),
                    start_to_close_timeout=_TIMEOUT,
                )

                batch_matched = not match_result.has_failures and len(match_result.matched) > 0
                if batch_matched:
                    batches_succeeded_total += 1
                else:
                    batches_failed_total += 1

                all_tally_results.append({
                    "batch_no": batch_no,
                    "invoice_name": inv_name,
                    "passed": batch_matched,
                    "batch_processed": True,
                })

        # ── Step 6: Tally ─────────────────────────────────────────────────
        # Count node: invoices (processed/succeeded/failed), batches (processed/succeeded/failed),
        # goods/line-items (failed only).
        # Resolved assumption: goods_failed = total failed line items (distinct, not per-field).
        tally_result = await workflow.execute_activity(
            tally,
            TallyInput(
                results=all_tally_results,
                count_keys=[
                    {
                        "collection": "invoice_processed",
                        "dedup_key": "invoice_name",
                        "label": "invoices",
                        "track": ["processed", "succeeded", "failed"],
                    },
                    {
                        "collection": "batch_processed",
                        "dedup_key": "batch_no",
                        "label": "batches",
                        "track": ["processed", "succeeded", "failed"],
                    },
                ],
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # Use our own aggregated counters (tally provides an alternative view)
        counts = tally_result.counts

        # Prefer tally counts if available, fall back to our accumulators
        inv_counts = counts.get("invoices", {})
        batch_counts = counts.get("batches", {})

        final_invoices_processed = inv_counts.get("processed", invoices_processed)
        final_invoices_succeeded = inv_counts.get("succeeded", invoices_succeeded)
        final_invoices_failed = inv_counts.get("failed", invoices_failed)
        final_batches_processed = batch_counts.get("processed", batches_processed_total)
        final_batches_succeeded = batch_counts.get("succeeded", batches_succeeded_total)
        final_batches_failed = batch_counts.get("failed", batches_failed_total)
        # goods_failed: distinct failed line items (one per failed invoice in this model)
        final_goods_failed = goods_failed_total

        # ── Step 7: Emit report ───────────────────────────────────────────
        # "Report results" node: one CSV row per shipment, 8 columns in order:
        # shipment_number, invoices_processed, invoices_succeeded, invoices_failed,
        # goods_failed, batches_processed, batches_succeeded, batches_failed.
        report_row = {
            "shipment_number": shipment_number,
            "invoices_processed": final_invoices_processed,
            "invoices_succeeded": final_invoices_succeeded,
            "invoices_failed": final_invoices_failed,
            "goods_failed": final_goods_failed,
            "batches_processed": final_batches_processed,
            "batches_succeeded": final_batches_succeeded,
            "batches_failed": final_batches_failed,
        }

        report_result = await workflow.execute_activity(
            emit_report,
            EmitReportInput(
                format="csv",
                columns=[
                    "shipment_number",
                    "invoices_processed",
                    "invoices_succeeded",
                    "invoices_failed",
                    "goods_failed",
                    "batches_processed",
                    "batches_succeeded",
                    "batches_failed",
                ],
                rows=[report_row],
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # ── Mark processed ─────────────────────────────────────────────────
        await workflow.execute_activity(
            mark_email_processed,
            EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
            start_to_close_timeout=_TIMEOUT,
        )

        return Board10AgentResult(
            message_id=inp.message_id,
            shipment_number=shipment_number,
            invoices_processed=final_invoices_processed,
            invoices_succeeded=final_invoices_succeeded,
            invoices_failed=final_invoices_failed,
            goods_failed=final_goods_failed,
            batches_processed=final_batches_processed,
            batches_succeeded=final_batches_succeeded,
            batches_failed=final_batches_failed,
            report_content=report_result.content,
        )