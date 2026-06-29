"""Pre-shipment email processing agent: validates commercial invoices and COA matching.

Generated from spec: Board 2
Frozen at: 2026-06-29T13:56:00.336828+00:00
DO NOT EDIT MANUALLY — regenerate via POST /api/v1/boards/81248e87-eaf8-4b2c-8ce3-fb1f6076aead/codegen
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

# ---------------------------------------------------------------------------
# Regulatory field labels — derived from spec required_fields / doc_fields.
# These are the five codes that must appear on EVERY product line in a
# commercial invoice.  (DUNS is explicitly excluded per resolved assumptions.)
# ---------------------------------------------------------------------------
_INVOICE_REQUIRED_FIELDS = [
    {"name": "HTS No",  "appears_as": "HTS No :"},
    {"name": "ANDA No", "appears_as": "ANDA No :"},
    {"name": "FDA No",  "appears_as": "FDA No :"},
    {"name": "Reg.No",  "appears_as": "Reg.No"},
    {"name": "NDC No",  "appears_as": "NDC No :"},
]

# Token pattern for batch number extraction (P2): mixed alphanumeric, 4+ chars.
_BATCH_TOKEN_RE = _re.compile(r'\b([A-Za-z0-9]{4,})\b')

# Regex to locate BATCH NOS label in invoice text (P2).
_BATCH_NOS_LABEL_RE = _re.compile(r'BATCH\s+NOS\s*:', _re.IGNORECASE)

# COA batch-identifier label variants (resolved assumption: accept Alias Batch No,
# Batch No, Lot No — all represent the same concept).
_COA_BATCH_LABEL_RE = _re.compile(
    r'(?:Alias\s+Batch\s+No|Batch\s+No|Lot\s+No)\.?[\s|:.]*([A-Za-z0-9]+)',
    _re.IGNORECASE,
)

# Status words that mark a COA as non-passing (P3 / Principle 4).
_QUARANTINE_WORDS = ("QUARANTINE", "HOLD", "REJECTED", "EMBARGOED", "VOID", "DRAFT")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_batch_numbers(doc_text: str) -> list[str]:
    """Extract batch numbers from invoice text using P2 robust token extraction.

    Scans both the text after 'BATCH NOS:' and the following line, collecting
    tokens that are mixed alphanumeric (contain both letters and digits), which
    distinguishes batch codes from pure-word descriptions and pure-digit counts.
    """
    lines = doc_text.splitlines()
    collected: list[str] = []
    for i, line in enumerate(lines):
        if _BATCH_NOS_LABEL_RE.search(line):
            colon_idx = line.find(":")
            after = line[colon_idx + 1:] if colon_idx != -1 else ""
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            search_text = after + " " + next_line
            for m in _BATCH_TOKEN_RE.finditer(search_text):
                tok = m.group(1)
                if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
                    collected.append(tok)
    return collected


def _count_line_item_results(doc_text: str) -> tuple[int, int]:
    """Return (passed_lines, failed_lines) based on P7 regulatory code coverage.

    Infers the number of product lines from how many times each code appears;
    never assumes code order or that any one label begins each line.
    """
    norm = " ".join(doc_text.split()).upper()
    counts = [norm.count(f["appears_as"].upper()) for f in _INVOICE_REQUIRED_FIELDS]
    n_lines = max(counts) if counts else 0
    if n_lines == 0:
        return (0, 1)   # no regulatory codes at all → one failing block
    passed = min(counts)
    failed = n_lines - passed
    return (passed, failed)


def _is_coa_quarantined(coa: dict) -> bool:
    """Return True if the COA's text or filename contains a quarantine/hold marker."""
    status_text = (coa.get("text", "") + " " + coa.get("name", "")).upper()
    return any(w in status_text for w in _QUARANTINE_WORDS)


def _extract_coa_identifier(coa: dict) -> str:
    """Return the batch identifier for a COA, preferring the classifier-extracted value."""
    ident = (coa.get("identifier") or "").strip()
    if ident:
        return ident
    # Fallback: scan text for label variants (P5 / resolved assumption)
    m = _COA_BATCH_LABEL_RE.search(coa.get("text", ""))
    if m:
        return m.group(1).strip()
    return ""


def _norm_batch(b: str) -> str:
    """Normalise a batch number for equivalence comparison."""
    return b.upper().strip()


def _batches_match(inv_batch: str, coa_batch: str) -> bool:
    """Return True if two batch identifiers refer to the same batch.

    Per resolved assumption: tolerate a trailing qualifier letter or suffix that
    one document may carry while the other omits (e.g. '1CV3U2601A' ↔ '1CV3U2601').
    Primary match: normalised equality; secondary: one is a prefix of the other.
    """
    a = _norm_batch(inv_batch)
    b = _norm_batch(coa_batch)
    if a == b:
        return True
    # One is a prefix of the other (trailing qualifier letter/suffix)
    if a.startswith(b) or b.startswith(a):
        return True
    return False


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Board2AgentInput:
    message_id: str = ""
    use_fixture: bool = True
    fixture_body: str = ""
    fixture_attachments: list[dict] = field(default_factory=list)
    board_id: str = ""
    fixture_subject: str = ""
    fixture_sender: str = ""


@dataclass
class Board2AgentResult:
    message_id: str
    shipment_number: str = ""
    shipment_number_missing: bool = False
    invoices_processed: int = 0
    invoices_succeeded: int = 0
    invoices_failed: int = 0
    goods_failed: int = 0
    batches_processed: int = 0
    batches_succeeded: int = 0
    batches_failed: int = 0
    report_content: str = ""


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@workflow.defn
class Board2AgentWorkflow:
    @workflow.run
    async def run(self, inp: Board2AgentInput) -> Board2AgentResult:

        # ── Dedup guard ────────────────────────────────────────────────────
        dedup = await workflow.execute_activity(
            is_email_processed,
            EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
            start_to_close_timeout=_TIMEOUT,
        )
        if dedup.already_processed:
            return Board2AgentResult(
                message_id=inp.message_id,
                shipment_number="",
                shipment_number_missing=True,
            )

        # ── Step 1: Fetch email + attachments (tool_action: fetch) ─────────
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

        # ── Step 2: Extract shipment facts from email (P4) ─────────────────
        # Invoice numbers appear after 'INV NO' / 'INVOICE NO' as a
        # comma-separated list in the form <CODE>/<period>/<number>
        # (e.g. U03/26-27/382).  The trailing number is the invoice number.
        # Shipment identifier is the MAWB/AWB or container/BL number.
        email_facts = await workflow.execute_activity(
            extract_email_facts,
            ExtractEmailFactsInput(
                subject=fetch_result.subject,
                body=fetch_result.body_text,
                shipment_id_hint=(
                    "MAWB or AWB number (format NNN-NNNNNNNN) or container/BL number; "
                    "found in subject or body of the pre-shipment alert email"
                ),
                invoice_hint=(
                    "Invoice numbers appear after 'INV NO' or 'INVOICE NO' as a "
                    "comma-separated list in the form <CODE>/<period>/<number> "
                    "(e.g. 'U03/26-27/382, 383').  Extract the trailing numeric portion "
                    "of each entry as the invoice number."
                ),
            ),
            start_to_close_timeout=_CLASSIFY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

        # Per resolved assumption: proceed without shipment number if absent.
        shipment_number_missing = not bool(email_facts.shipment_id)
        shipment_number = email_facts.shipment_id or fetch_result.subject.strip()
        invoice_numbers = email_facts.invoice_numbers

        # ── Step 3: Classify all attachments by content (P3) ──────────────
        classify_result = await workflow.execute_activity(
            classify_documents,
            ClassifyDocumentsInput(
                attachments=fetch_result.attachments,
                doc_types=[
                    {
                        "type_name": "commercial_invoice",
                        "description": (
                            "A commercial invoice for a pharmaceutical shipment. "
                            "Text must contain regulatory validation codes present on "
                            "each product line: HTS No (harmonised tariff schedule), "
                            "ANDA No (abbreviated new drug application number), "
                            "FDA No, Reg.No (registration number, may be preceded by FEI), "
                            "and NDC No (national drug code).  The presence of two or more "
                            "of these codes is the decisive signal.  Packing lists covering "
                            "the same goods do NOT contain these regulatory codes."
                        ),
                    },
                    {
                        "type_name": "certificate_of_analysis",
                        "description": (
                            "A certificate reporting laboratory test results or specification "
                            "values for a specific manufactured batch or lot of product. "
                            "It states a batch or lot identifier — labelled in ways that vary "
                            "by sender, e.g. 'Alias Batch No.', 'Batch No.', 'Lot No.' — "
                            "alongside test parameters and their pass/fail or numerical results. "
                            "Qualify any document that clearly serves this purpose even if the "
                            "exact example label is not present.  These are often scanned PDFs "
                            "whose filename contains a batch number."
                        ),
                    },
                    {
                        "type_name": "packing_list",
                        "description": (
                            "Packing or carton list: shows item counts, quantities, and weights "
                            "for the same shipment goods but does NOT contain regulatory "
                            "validation codes (HTS No, ANDA No, FDA No, Reg.No, NDC No). "
                            "Filename often includes 'PL' (e.g. X-382-PL.pdf or X-382 PL.pdf)."
                        ),
                    },
                    {
                        "type_name": "other",
                        "description": (
                            "Logistics, transport, bill of lading, airway bill, or any "
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

        # ── Step 4: Reconcile invoice_numbers with classified invoice docs (P4) ──
        invoices_in_shipment: list[dict] = []
        if invoice_numbers:
            remaining = list(invoice_docs)
            for inv_num in invoice_numbers:
                def _content_match(d, _n=inv_num):
                    ident = (d.get("identifier") or "").strip()
                    text = d.get("text", "")
                    name = d.get("name", "")
                    # (a) Precise identifier match: last segment equality
                    if ident:
                        segs = _re.split(r'[/\-\s]+', ident)
                        if segs[-1] == _n:
                            return True
                    # (b) Bounded text match: inv_num as a distinct numeric token
                    if text and _re.search(r'(?<!\d)' + _re.escape(_n) + r'(?![\-\d])', text):
                        return True
                    # (c) Same bounded check on the filename
                    if name and _re.search(r'(?<!\d)' + _re.escape(_n) + r'(?![\-\d])', name):
                        return True
                    return False

                matched = next((d for d in remaining if _content_match(d)), None)
                if matched:
                    invoices_in_shipment.append(matched)
                    remaining.remove(matched)
            if not invoices_in_shipment:
                # None matched by content — fall back to all classified invoices
                invoices_in_shipment = invoice_docs
        else:
            invoices_in_shipment = invoice_docs

        # ── Step 5: Process each invoice (outer for-each loop) ─────────────
        # Per-shipment accumulators
        invoices_processed = 0
        invoices_succeeded = 0
        invoices_failed = 0
        goods_failed_total = 0
        batches_processed_total = 0
        batches_succeeded_total = 0
        batches_failed_total = 0

        # Collect tally rows for emit_report / tally activities
        tally_rows: list[dict] = []

        for inv in invoices_in_shipment:
            invoices_processed += 1
            inv_text = inv.get("text", "")
            inv_name = inv.get("name", inv.get("identifier", "invoice"))

            # ── Step 5a: Validate regulatory fields on the invoice (P1 + P7) ──
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

            # Count line-item-level goods failures (P7 + resolved assumption:
            # one failure per product line missing/invalid on any of the five codes).
            passed_lines, failed_lines = _count_line_item_results(inv_text)
            goods_failed_total += failed_lines

            # Invoice passes only if all five codes are present on every product line.
            invoice_passed = inv_validation.passed and (failed_lines == 0)

            # ── Step 5b: Extract batch numbers from this invoice (P2) ───────
            batch_numbers = _extract_batch_numbers(inv_text)

            # ── Step 5c: Match each batch to its COA (inner for-each loop) ──
            # Per resolved assumption: collect per-invoice and match only this
            # invoice's batches to COAs — do NOT pool across invoices.
            inv_batches_processed = 0
            inv_batches_succeeded = 0
            inv_batches_failed = 0

            for batch_no in batch_numbers:
                inv_batches_processed += 1

                # Primary match: use identifier already extracted by classify_documents.
                # Equivalence: tolerate trailing qualifier letter / suffix (resolved assumption).
                matched_coa = None
                for c in coa_docs:
                    coa_ident = _extract_coa_identifier(c)
                    if coa_ident and _batches_match(batch_no, coa_ident):
                        matched_coa = c
                        break

                # Fallback: search coa text for the batch number when LLM identifier was empty.
                if matched_coa is None:
                    for c in coa_docs:
                        coa_ident = _extract_coa_identifier(c)
                        if not coa_ident:
                            # P5 literal fallback: batch number appears anywhere in COA text
                            if _norm_batch(batch_no) in c.get("text", "").upper():
                                matched_coa = c
                                break

                if matched_coa is not None and not _is_coa_quarantined(matched_coa):
                    inv_batches_succeeded += 1
                    batch_status = "succeeded"
                else:
                    inv_batches_failed += 1
                    batch_status = "failed"

                tally_rows.append({
                    "invoice": inv_name,
                    "batch_no": batch_no,
                    "batch_status": batch_status,
                    "passed": matched_coa is not None and not _is_coa_quarantined(matched_coa),
                })

            batches_processed_total += inv_batches_processed
            batches_succeeded_total += inv_batches_succeeded
            batches_failed_total += inv_batches_failed

            # Invoice also fails if any of its batches has no valid COA.
            if inv_batches_failed > 0:
                invoice_passed = False

            if invoice_passed:
                invoices_succeeded += 1
            else:
                invoices_failed += 1

            tally_rows.append({
                "invoice": inv_name,
                "invoice_status": "succeeded" if invoice_passed else "failed",
                "passed": invoice_passed,
            })

        # ── Step 6: Tally counts ───────────────────────────────────────────
        tally_result = await workflow.execute_activity(
            tally,
            TallyInput(
                results=tally_rows,
                count_keys=[
                    {
                        "collection": "invoice_status",
                        "dedup_key": "invoice",
                        "label": "invoices",
                        "track": ["succeeded", "failed"],
                    },
                    {
                        "collection": "batch_status",
                        "dedup_key": "batch_no",
                        "label": "batches",
                        "track": ["succeeded", "failed"],
                    },
                ],
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # ── Step 7: Emit report (one CSV row per shipment) ─────────────────
        report_row = {
            "shipment_number": shipment_number,
            "shipment_number_missing": str(shipment_number_missing),
            "invoices_processed": str(invoices_processed),
            "invoices_succeeded": str(invoices_succeeded),
            "invoices_failed": str(invoices_failed),
            "goods_failed": str(goods_failed_total),
            "batches_processed": str(batches_processed_total),
            "batches_succeeded": str(batches_succeeded_total),
            "batches_failed": str(batches_failed_total),
        }

        emit_result = await workflow.execute_activity(
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

        return Board2AgentResult(
            message_id=inp.message_id,
            shipment_number=shipment_number,
            shipment_number_missing=shipment_number_missing,
            invoices_processed=invoices_processed,
            invoices_succeeded=invoices_succeeded,
            invoices_failed=invoices_failed,
            goods_failed=goods_failed_total,
            batches_processed=batches_processed_total,
            batches_succeeded=batches_succeeded_total,
            batches_failed=batches_failed_total,
            report_content=emit_result.content,
        )