"""Shipment pre-alert processing workflow: validate commercial invoices and match COAs.

Generated from spec: Board 11
Frozen at: 2026-06-29T08:14:31.140703+00:00
DO NOT EDIT MANUALLY — regenerate via POST /api/v1/boards/02c25383-2add-43cd-958f-d679af84d9e4/codegen
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
_VISION_TIMEOUT = timedelta(seconds=180)
_CLASSIFY_TIMEOUT = timedelta(minutes=10)

# ── Required regulatory fields for each invoice product line ────────────────
# These are the five codes that must appear per product line; BATCH NOS is
# collected separately for batch matching and is NOT a pass/fail field.
# (Resolved assumption: DUNS is intentionally ignored.)
_INVOICE_REQUIRED_FIELDS = [
    {"name": "HTS code",            "appears_as": "HTS No",   "required": True},
    {"name": "ANDA number",         "appears_as": "ANDA No",  "required": True},
    {"name": "FDA number",          "appears_as": "FDA No",   "required": True},
    {"name": "Registration number", "appears_as": "Reg.No",   "required": True},
    {"name": "NDC number",          "appears_as": "NDC No",   "required": True},
]

# Token pattern for batch-number extraction (P2): mixed alphanumeric, 6+ chars
_BATCH_TOKEN_RE = _re.compile(r'\b([A-Za-z0-9]{6,})\b')

# COA batch-identifier extraction (P5): tolerant label variants + separator forms
_ALIAS_BATCH_RE = _re.compile(
    r'(?:Alias\s+Batch\s+No|Batch\s+No|Lot\s+No)\.?[\s|:.]*([A-Za-z0-9]+)',
    _re.IGNORECASE,
)

# Status markers that indicate a COA is quarantined / invalid (Principle 4)
_QUARANTINE_WORDS = ("QUARANTINE", "HOLD", "REJECTED", "EMBARGOED", "VOID", "DRAFT")


def _extract_batch_numbers(doc_text: str) -> list[str]:
    """Extract batch numbers from an invoice's BATCH NOS field (P2 pattern).

    Scans both the text after the BATCH NOS colon AND the following line, then
    filters to mixed-alphanumeric tokens (rejects pure-digit and pure-alpha).
    Deduplicates while preserving order.
    """
    lines = doc_text.splitlines()
    collected: list[str] = []
    for i, line in enumerate(lines):
        if "BATCH NOS" in line.upper() or "BATCH NO." in line.upper():
            after = ""
            colon_idx = line.find(":")
            if colon_idx != -1:
                after = line[colon_idx + 1:]
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            search_text = after + " " + next_line
            for m in _BATCH_TOKEN_RE.finditer(search_text):
                tok = m.group(1)
                if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
                    if tok not in collected:
                        collected.append(tok)
    return collected


def _count_line_item_results(doc_text: str) -> tuple[int, int]:
    """Return (passed_lines, failed_lines) by regulatory code occurrence counts (P7).

    Counts how many times each required code appears; infers line count from the
    maximum occurrence, and passed count from the minimum (full coverage).
    """
    norm = " ".join(doc_text.split()).upper()
    labels = [f["appears_as"].upper() for f in _INVOICE_REQUIRED_FIELDS]
    counts = [norm.count(lbl) for lbl in labels]
    n_lines = max(counts) if counts else 0
    if n_lines == 0:
        return (0, 1)  # no regulatory codes at all → one failing block
    passed = min(counts)
    failed = n_lines - passed
    return (passed, failed)


def _is_coa_quarantined(coa: dict) -> bool:
    """Return True if content or filename indicates the COA is quarantined/void."""
    status_text = (coa.get("text", "") + " " + coa.get("name", "")).upper()
    return any(w in status_text for w in _QUARANTINE_WORDS)


def _extract_coa_batch_identifiers(coa: dict) -> list[str]:
    """Extract all Alias Batch No. values from a COA document (content-first, P5).

    A single multi-page COA PDF may carry batch numbers for several batches
    (resolved assumption: one file can match multiple batches).
    """
    # Prefer the single identifier already extracted by classify_documents
    primary = (coa.get("identifier") or "").strip()
    identifiers: list[str] = []
    if primary:
        identifiers.append(primary.upper())

    # Also scan the full text for additional Alias Batch No. occurrences
    text = coa.get("text", "")
    for m in _ALIAS_BATCH_RE.finditer(text):
        val = m.group(1).strip().upper()
        if val and val not in identifiers:
            identifiers.append(val)

    return identifiers


def _normalize_batch(batch_no: str) -> str:
    """Normalize a batch identifier for equivalence comparison."""
    return batch_no.strip().upper()


def _batches_equivalent(a: str, b: str) -> bool:
    """True if two batch identifiers refer to the same real-world batch.

    Handles:
    - Exact match after normalization
    - One is a prefix of the other (trailing qualifier letter/suffix difference,
      e.g. '1CV3U2601A' on invoice matches '1CV3U2601' on COA or vice versa)
    """
    na, nb = _normalize_batch(a), _normalize_batch(b)
    if na == nb:
        return True
    # Trailing qualifier: one is the other with a single letter appended
    if na.startswith(nb) and len(na) - len(nb) <= 2:
        return True
    if nb.startswith(na) and len(nb) - len(na) <= 2:
        return True
    return False


def _find_coa_for_batch(batch_no: str, coa_docs: list[dict]) -> dict | None:
    """Find the first non-quarantined COA that carries the given batch number.

    Resolved assumption: identify COA by content first (Alias Batch No. field);
    fall back to filename only if content read failed (text is empty).
    Resolved assumption: one PDF may contain COAs for multiple batches.
    """
    norm = _normalize_batch(batch_no)

    for coa in coa_docs:
        # Content-first: check all batch identifiers extracted from this COA
        coa_batch_ids = _extract_coa_batch_identifiers(coa)
        for coa_id in coa_batch_ids:
            if _batches_equivalent(norm, coa_id):
                return coa  # return even if quarantined (caller checks separately)

        # Fallback: if content read yielded nothing, check filename
        if not coa_batch_ids and norm in coa.get("name", "").upper():
            return coa

    return None


@dataclass
class Board11AgentInput:
    message_id: str = ""
    use_fixture: bool = True
    fixture_body: str = ""
    fixture_attachments: list[dict] = field(default_factory=list)
    board_id: str = ""
    fixture_subject: str = ""
    fixture_sender: str = ""


@dataclass
class Board11AgentResult:
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


@workflow.defn
class Board11AgentWorkflow:
    @workflow.run
    async def run(self, inp: Board11AgentInput) -> Board11AgentResult:

        # ── Dedup guard ────────────────────────────────────────────────────
        dedup = await workflow.execute_activity(
            is_email_processed,
            EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
            start_to_close_timeout=_TIMEOUT,
        )
        if dedup.already_processed:
            return Board11AgentResult(
                message_id=inp.message_id,
                report_content="already_processed",
            )

        # ── Fetch email and attachments (tool_action: fetch) ───────────────
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

        # ── Extract shipment ID and invoice list from email (P4) ───────────
        # Shipment identifier may be a MAWB (NNN-NNNNNNNN), sea container/BL,
        # or any identifier in the subject/body.  Invoice numbers may be
        # abbreviated (e.g. "100/26-27/235, 238" → ["235", "238"]).
        email_facts = await workflow.execute_activity(
            extract_email_facts,
            ExtractEmailFactsInput(
                subject=fetch_result.subject,
                body=fetch_result.body_text,
                shipment_id_hint=(
                    "Shipment identifier — may be an air waybill (MAWB/AWB, format NNN-NNNNNNNN), "
                    "a sea container number, or a bill-of-lading (BL) number; varies by sender"
                ),
                invoice_hint=(
                    "Invoice numbers may appear after an 'INVOICE NO' label and may be "
                    "abbreviated comma-separated forms (e.g. '100/26-27/235, 238' expands "
                    "to invoices 235 and 238); use '' if no invoice list is present"
                ),
            ),
            start_to_close_timeout=_CLASSIFY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        shipment_number = email_facts.shipment_id or fetch_result.subject.strip()
        invoice_numbers = email_facts.invoice_numbers

        # ── Classify all attachments by content (P3) ───────────────────────
        classify_result = await workflow.execute_activity(
            classify_documents,
            ClassifyDocumentsInput(
                attachments=fetch_result.attachments,
                doc_types=[
                    {
                        "type_name": "commercial_invoice",
                        "description": (
                            "A commercial invoice for a pharmaceutical shipment. "
                            "Text must contain at least one of the regulatory validation codes "
                            "required per product line: HTS No, ANDA No, FDA No, Reg.No, NDC No. "
                            "These codes are absent from packing lists covering the same goods — "
                            "their presence is the decisive test. Also typically contains a "
                            "BATCH NOS field listing batch numbers. Does NOT qualify as a COA."
                        ),
                    },
                    {
                        "type_name": "certificate_of_analysis",
                        "description": (
                            "A certificate reporting laboratory test results or specification "
                            "values for a specific manufactured batch or lot of product. "
                            "It states a batch or lot identifier — labelled in ways that VARY "
                            "by sender, e.g. 'Alias Batch No.', 'Batch No.', 'Lot No.' — "
                            "alongside test parameters and their pass/fail or specification "
                            "results. Often a scanned document. Qualify any document that "
                            "clearly serves this purpose even if the exact example label is "
                            "not present. Does NOT contain HTS No / ANDA No / FDA No / NDC No."
                        ),
                    },
                    {
                        "type_name": "packing_list",
                        "description": (
                            "Packing or carton list: shows item counts, quantities, and weights "
                            "for the same shipment goods but does NOT contain regulatory "
                            "validation codes (HTS No, ANDA No, FDA No, Reg.No, NDC No). "
                            "Identify by this ABSENCE of regulatory codes, not by 'PL' in "
                            "the filename."
                        ),
                    },
                    {
                        "type_name": "other",
                        "description": (
                            "Logistics, transport, bill-of-lading, air-waybill, or any "
                            "uncategorised document."
                        ),
                    },
                ],
            ),
            start_to_close_timeout=_CLASSIFY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

        invoice_docs = [
            d for d in classify_result.documents
            if d["doc_type"] == "commercial_invoice"
        ]
        coa_docs = [
            d for d in classify_result.documents
            if d["doc_type"] == "certificate_of_analysis"
        ]
        # Packing lists are explicitly excluded from all processing
        # (resolved assumption: ignore packing list batch numbers entirely)

        # ── Reconcile email invoice list with classified docs (P4) ─────────
        invoices_in_shipment: list[dict] = []
        if invoice_numbers:
            remaining = list(invoice_docs)
            for inv_num in invoice_numbers:
                def _content_match(d, _n=inv_num):
                    ident = (d.get("identifier") or "").strip()
                    text  = d.get("text", "")
                    name  = d.get("name", "")
                    # (a) Last-segment equality on classifier-extracted identifier
                    if ident:
                        segs = _re.split(r'[/\-\s]+', ident)
                        if segs[-1] == _n:
                            return True
                    # (b) Bounded text match — invoice number as distinct numeric token
                    if text and _re.search(
                        r'(?<!\d)' + _re.escape(_n) + r'(?![\-\d])', text
                    ):
                        return True
                    # (c) Bounded filename match (last resort)
                    if name and _re.search(
                        r'(?<!\d)' + _re.escape(_n) + r'(?![\-\d])', name
                    ):
                        return True
                    return False

                matched = next((d for d in remaining if _content_match(d)), None)
                if matched:
                    invoices_in_shipment.append(matched)
                    remaining.remove(matched)

            if not invoices_in_shipment:
                # No invoice matched by content — fall back to all classified invoices
                invoices_in_shipment = invoice_docs
        else:
            # No invoice list in email — trust strict content classification
            invoices_in_shipment = invoice_docs

        # ── Outer loop: for each invoice in the shipment ───────────────────
        # Counters accumulated across all invoices
        invoices_processed = 0
        invoices_succeeded = 0
        invoices_failed    = 0
        goods_failed_total = 0

        # Batch-level accumulators (dedup across invoices)
        all_batch_nos: list[str] = []         # ordered, deduped
        batch_pass_map: dict[str, bool] = {}  # batch_no → passed

        all_inv_results: list[dict] = []      # for tally
        all_batch_results: list[dict] = []    # for tally

        for inv in invoices_in_shipment:
            invoices_processed += 1
            inv_text = inv.get("text", "")
            inv_name = inv.get("name", "")

            # ── Check the invoice: validate required regulatory fields (P1) ─
            normalised_inv_text = " ".join(inv_text.split())
            inv_validation = await workflow.execute_activity(
                validate_required_fields,
                ValidateFieldsInput(
                    document_text=normalised_inv_text,
                    fields=_INVOICE_REQUIRED_FIELDS,
                    fail_if="any_missing",
                    applies_to="per_document",
                ),
                start_to_close_timeout=_TIMEOUT,
            )

            # ── Count failed product lines (P7, resolved assumption) ────────
            # Each product line is checked separately; goods_failed counts
            # lines missing at least one code — not whole invoices, not fields.
            line_passed, line_failed = _count_line_item_results(inv_text)
            goods_failed_total += line_failed

            # Invoice succeeds only if ALL product lines pass
            inv_passed = inv_validation.passed and (line_failed == 0)
            if inv_passed:
                invoices_succeeded += 1
            else:
                invoices_failed += 1

            all_inv_results.append({
                "invoice_name": inv_name,
                "passed": inv_passed,
            })

            # ── Extract batch numbers from this invoice (P2) ────────────────
            # Only batch numbers from commercial invoices are used
            # (resolved assumption: ignore packing list batch numbers entirely)
            inv_batch_nos = _extract_batch_numbers(inv_text)

            # Fallback: if P2 yielded nothing, try whitespace-normalised text
            if not inv_batch_nos:
                inv_batch_nos = _extract_batch_numbers(normalised_inv_text)

            # ── Inner loop: for each batch on this invoice ──────────────────
            for batch_no in inv_batch_nos:
                # Dedup batches across invoices
                if batch_no not in all_batch_nos:
                    all_batch_nos.append(batch_no)

                if batch_no in batch_pass_map:
                    # Already evaluated (e.g. same batch on multiple invoices)
                    continue

                # ── Match batch to COA (content-first, resolved assumption) ──
                # Resolved assumption: COA identified by 'Alias Batch No.' in
                # scanned content; filename fallback only if content unreadable.
                # Resolved assumption: one PDF may match multiple batches.
                coa = _find_coa_for_batch(batch_no, coa_docs)

                if coa is not None:
                    quarantined = _is_coa_quarantined(coa)
                    batch_passed = not quarantined
                else:
                    batch_passed = False

                batch_pass_map[batch_no] = batch_passed

        # ── Build batch source/target lists for match_by_key ──────────────
        # Use match_by_key to produce a canonical MatchResult for tally
        source_items = [{"batch_no": b} for b in all_batch_nos]
        target_items = [
            {"batch_no": b}
            for b, ok in batch_pass_map.items()
            if ok
        ]

        match_result = await workflow.execute_activity(
            match_by_key,
            MatchInput(
                source_items=source_items,
                target_items=target_items,
                key_field="batch_no",
                on_missing="fail",
                match_type="normalized",
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        batches_processed  = len(all_batch_nos)
        batches_succeeded  = len(match_result.matched)
        batches_failed     = len(match_result.unmatched_source)

        # ── Tally ──────────────────────────────────────────────────────────
        # Assemble result rows for tally activity
        tally_rows: list[dict] = []
        for r in all_inv_results:
            tally_rows.append({
                "invoice_name": r["invoice_name"],
                "passed": r["passed"],
            })
        for b in all_batch_nos:
            tally_rows.append({
                "batch_no": b,
                "passed": batch_pass_map.get(b, False),
            })

        tally_result = await workflow.execute_activity(
            tally,
            TallyInput(
                results=tally_rows,
                count_keys=[
                    {
                        "collection": "invoice_name",
                        "dedup_key": "invoice_name",
                        "label": "invoices",
                        "track": ["succeeded", "failed", "processed"],
                    },
                    {
                        "collection": "batch_no",
                        "dedup_key": "batch_no",
                        "label": "batches",
                        "track": ["succeeded", "failed", "processed"],
                    },
                ],
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # ── Report results ─────────────────────────────────────────────────
        # One CSV row per shipment with eight columns
        # (resolved assumption: displaying CSV output is sufficient)
        report_row = {
            "shipment_number":    shipment_number,
            "invoices_processed": invoices_processed,
            "invoices_succeeded": invoices_succeeded,
            "invoices_failed":    invoices_failed,
            "goods_failed":       goods_failed_total,
            "batches_processed":  batches_processed,
            "batches_succeeded":  batches_succeeded,
            "batches_failed":     batches_failed,
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

        return Board11AgentResult(
            message_id=inp.message_id,
            shipment_number=shipment_number,
            invoices_processed=invoices_processed,
            invoices_succeeded=invoices_succeeded,
            invoices_failed=invoices_failed,
            goods_failed=goods_failed_total,
            batches_processed=batches_processed,
            batches_succeeded=batches_succeeded,
            batches_failed=batches_failed,
            report_content=report_result.content,
        )