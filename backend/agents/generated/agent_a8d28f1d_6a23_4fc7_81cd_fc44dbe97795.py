"""Shipment pre-alert validation workflow: invoice goods validation and batch-to-COA matching.

Generated from spec: Board 7
Frozen at: 2026-06-28T00:09:13.790716+00:00
DO NOT EDIT MANUALLY — regenerate via POST /api/v1/boards/a8d28f1d-6a23-4fc7-81cd-fc44dbe97795/codegen
"""
import asyncio
import re as _re
from dataclasses import dataclass, field
from datetime import timedelta
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from backend.runtime.activities.fetch_email import fetch_email_and_attachments
    from backend.runtime.activities.load_document import load_document
    from backend.runtime.activities.validate_fields import validate_required_fields
    from backend.runtime.activities.match_documents import match_by_key
    from backend.runtime.activities.tally import tally
    from backend.runtime.activities.report import emit_report, send_report
    from backend.runtime.activities.email_dedup import is_email_processed, mark_email_processed
    from backend.runtime.activities._types import (
        FetchEmailInput, LoadDocumentInput, ValidateFieldsInput,
        MatchInput, TallyInput, EmitReportInput, SendReportInput,
        EmailDedupInput,
    )

_TIMEOUT = timedelta(seconds=60)
_VISION_TIMEOUT = timedelta(seconds=180)  # load_document calls that may invoke Claude vision

# Invoice filename pattern: U06-{digits}.pdf — rejects packing list variants like U06-335-PL.pdf
_INVOICE_RE = _re.compile(r'U06-\d+\.pdf$', _re.IGNORECASE)

# Token pattern for batch number extraction from BATCH NOS: field (P2)
# Batch numbers are alphanumeric, 7+ chars, start with a letter, contain at least one digit
_TOKEN_RE = _re.compile(r'\b([A-Za-z][A-Za-z0-9]{6,})\b')

# Alias Batch No. extraction from COA text (P5)
_ALIAS_BATCH_RE = _re.compile(
    r'Alias\s+Batch\s+No\.?[\s|:.]*([A-Za-z0-9]+)',
    _re.IGNORECASE,
)


def _extract_batch_numbers(invoice_text: str) -> list[str]:
    """Extract deduplicated batch numbers from BATCH NOS: fields in an invoice (P2)."""
    lines = invoice_text.splitlines()
    collected: list[str] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        if "BATCH NOS" in line.upper():
            colon_idx = line.find(":")
            after = line[colon_idx + 1:] if colon_idx != -1 else ""
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            search_text = after + " " + next_line
            for m in _TOKEN_RE.finditer(search_text):
                tok = m.group(1)
                # Require at least one digit — rejects pure English description words
                if any(c.isdigit() for c in tok) and tok not in seen:
                    seen.add(tok)
                    collected.append(tok)
    return collected


def _extract_alias_batch_no(coa_text: str, source_key: str) -> str:
    """Extract Alias Batch No. from COA text with literal fallback (P5)."""
    m = _ALIAS_BATCH_RE.search(coa_text)
    if m:
        return m.group(1).strip()
    # Literal fallback: if the source batch number appears in the COA text, use it
    if source_key and source_key in coa_text:
        return source_key
    return ""


@dataclass
class Board7AgentInput:
    message_id: str = ""
    use_fixture: bool = True
    fixture_body: str = ""
    fixture_attachments: list[dict] = field(default_factory=list)
    board_id: str = ""
    fixture_subject: str = ""   # eval/fixture runs set the shipment key via this field
    fixture_sender: str = ""


@dataclass
class Board7AgentResult:
    message_id: str
    shipment_number: str = ""
    invoices_processed: int = 0
    invoices_succeeded: int = 0
    invoices_failed: int = 0
    batches_processed: int = 0
    batches_succeeded: int = 0
    batches_failed: int = 0
    goods_failed: int = 0
    report_content: str = ""


@workflow.defn
class Board7AgentWorkflow:
    @workflow.run
    async def run(self, inp: Board7AgentInput) -> Board7AgentResult:
        # ── Dedup guard ────────────────────────────────────────────────────
        dedup = await workflow.execute_activity(
            is_email_processed,
            EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
            start_to_close_timeout=_TIMEOUT,
        )
        if dedup.already_processed:
            return Board7AgentResult(
                message_id=inp.message_id,
                shipment_number="",
            )

        # ── Fetch email and all attachments ────────────────────────────────
        # Resolved assumption: shipment_number (MAWB/container) comes from subject (P4)
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

        # Extract shipment number from subject (P4)
        # Subject carries the MAWB/container number directly for fixture and live runs
        shipment_number = fetch_result.subject.strip()

        # ── Identify invoice attachments (P3) ──────────────────────────────
        # Resolved assumption: invoice files match U06-{digits}.pdf pattern.
        # Excludes transport docs (MAWB.pdf, HAWB.pdf, MANIFEST.pdf),
        # packing lists (*-PL.pdf, *.xlsx), and COA files (named by batch number).
        invoice_attachments = [
            att for att in fetch_result.attachments
            if _INVOICE_RE.search(att.get("name", ""))
        ]

        # ── Pre-load all COA attachments concurrently (P6) ─────────────────
        # COAs are identified by filename containing the batch number (resolved assumption).
        # We load them on demand per batch during the inner loop, cached to avoid re-reads.
        # Cache: batch_number → LoadDocumentResult
        _coa_cache: dict = {}

        # ── Per-shipment result accumulators ───────────────────────────────
        all_tally_results: list[dict] = []

        # ── Outer loop: for each Invoice in the shipment ───────────────────
        for inv_att in invoice_attachments:
            inv_filename = inv_att.get("name", "")

            # Load the commercial invoice document
            # identified_by="filename", identifier = full filename (P rule: use full name)
            invoice_doc = await workflow.execute_activity(
                load_document,
                LoadDocumentInput(
                    attachments=fetch_result.attachments,
                    identified_by="filename",
                    identifier=inv_filename,
                ),
                start_to_close_timeout=_VISION_TIMEOUT,
            )

            invoice_text = invoice_doc.text
            # Derive invoice number from filename (e.g. "U06-335.pdf" → "U06-335")
            invoice_number = inv_filename.rsplit(".", 1)[0] if invoice_doc.found else inv_filename

            # ── Validate Goods (per invoice, checks five regulatory codes) ──
            # Resolved assumption: fields are HTS, ANDA, FDA, REG NO (labeled "Reg.No"), NDC
            # applies_to="per_line_item": each non-empty line checked independently
            # Whitespace-normalised copy for field matching (P1)
            normalised_invoice_text = " ".join(invoice_text.split())

            validate_goods = await workflow.execute_activity(
                validate_required_fields,
                ValidateFieldsInput(
                    document_text=normalised_invoice_text,
                    fields=[
                        {"name": "HTS",    "appears_as": "HTS No",  "scope": "line_item", "required": True},
                        {"name": "ANDA",   "appears_as": "ANDA No", "scope": "line_item", "required": True},
                        {"name": "FDA",    "appears_as": "FDA No",  "scope": "line_item", "required": True},
                        # Resolved assumption: REG NO field labeled "Reg.No" on the invoice
                        {"name": "REG NO", "appears_as": "Reg.No",  "scope": "line_item", "required": True},
                        {"name": "NDC",    "appears_as": "NDC No",  "scope": "line_item", "required": True},
                    ],
                    fail_if="any_missing",
                    applies_to="per_line_item",
                ),
                start_to_close_timeout=_TIMEOUT,
            )

            # Count failed goods (goods_failed = number of required fields missing across line items)
            # A "good" fails if any of its five codes is absent; track only failed per spec.
            goods_failed_count = sum(
                1 for fr in validate_goods.field_results
                if not fr.get("found", True)
            )

            # Record invoice-level tally entry
            invoice_result_dict = {
                "invoices": invoice_number,
                "invoice number": invoice_number,
                "passed": validate_goods.passed,
                "goods_failed": goods_failed_count,
                "shipment_number": shipment_number,
            }

            # ── Extract batch numbers from BATCH NOS: fields on this invoice (P2) ──
            # Resolved assumption: batch numbers are in "BATCH NOS:" field on each line item
            batch_numbers = _extract_batch_numbers(invoice_text)

            # ── Pre-load uncached COA documents concurrently (P6) ──────────
            # COA filename convention: "{batch_number} COA.pdf" (resolved assumption)
            uncached_batches = [bn for bn in batch_numbers if bn not in _coa_cache]
            if uncached_batches:
                _coa_tasks = [
                    workflow.execute_activity(
                        load_document,
                        LoadDocumentInput(
                            attachments=fetch_result.attachments,
                            identified_by="filename",
                            # Identifier = batch number; COA files named "{batch} COA.pdf"
                            identifier=bn,
                        ),
                        start_to_close_timeout=_VISION_TIMEOUT,
                    )
                    for bn in uncached_batches
                ]
                _coa_results = await asyncio.gather(*_coa_tasks)
                for bn, coa_res in zip(uncached_batches, _coa_results):
                    _coa_cache[bn] = coa_res

            # ── Inner loop: for each batch on the invoice ──────────────────
            for batch_number in batch_numbers:
                coa_doc = _coa_cache[batch_number]

                # Extract Alias Batch No. from COA (resolved assumption: COA carries
                # batch number as "Alias Batch No." field, e.g. ULXDA26012A)
                coa_alias_batch = _extract_alias_batch_no(coa_doc.text, batch_number)

                # Build source and target items for match_by_key
                source_item = {"batch number": batch_number}
                target_item = {"batch number": coa_alias_batch} if coa_alias_batch else {}

                # Match batch to COA — exact match on batch number
                # Resolved assumption: a batch with no matching COA Alias Batch No. = failed
                match_batch_to_coa = await workflow.execute_activity(
                    match_by_key,
                    MatchInput(
                        source_items=[source_item],
                        target_items=[target_item] if target_item else [],
                        key_field="batch number",
                        on_missing="fail",
                        match_type="exact",
                    ),
                    start_to_close_timeout=_TIMEOUT,
                )

                batch_passed = not match_batch_to_coa.has_failures
                batch_result_dict = {
                    "batches": batch_number,
                    "batch number": batch_number,
                    "invoice number": invoice_number,
                    "passed": batch_passed,
                    "shipment_number": shipment_number,
                }
                all_tally_results.append(batch_result_dict)

            # Append invoice result after inner loop
            all_tally_results.append(invoice_result_dict)

        # ── Roll up results per shipment ───────────────────────────────────
        # Three grains: invoices, batches, goods (goods track only failed per spec)
        roll_up_results_per_shipment = await workflow.execute_activity(
            tally,
            TallyInput(
                results=all_tally_results,
                count_keys=[
                    {
                        "collection": "invoices",
                        "dedup_key": "invoice number",
                        "label": "invoices",
                        "track": ["processed", "succeeded", "failed"],
                    },
                    {
                        "collection": "batches",
                        "dedup_key": "batch number",
                        "label": "batches",
                        "track": ["processed", "succeeded", "failed"],
                    },
                    {
                        "collection": "goods",
                        "dedup_key": "good / line item",
                        "label": "goods",
                        # Resolved assumption: goods track only "failed" state
                        "track": ["failed"],
                    },
                ],
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        counts = roll_up_results_per_shipment.counts

        # Extract tally values with safe defaults
        inv_counts = counts.get("invoices", {})
        bat_counts = counts.get("batches", {})
        goo_counts = counts.get("goods", {})

        # Compute goods_failed from accumulated per-invoice goods_failed_count values
        # (tally may not have a "goods" grain populated since we didn't emit goods-grain rows;
        # fall back to summing the goods_failed fields from invoice result entries)
        goods_failed_total = sum(
            r.get("goods_failed", 0)
            for r in all_tally_results
            if "invoices" in r
        )
        # If tally did produce a goods failed count, prefer it
        if goo_counts.get("failed", 0) > 0:
            goods_failed_total = goo_counts["failed"]

        invoices_processed  = inv_counts.get("processed",  0)
        invoices_succeeded  = inv_counts.get("succeeded",  0)
        invoices_failed     = inv_counts.get("failed",     0)
        batches_processed   = bat_counts.get("processed",  0)
        batches_succeeded   = bat_counts.get("succeeded",  0)
        batches_failed      = bat_counts.get("failed",     0)

        # ── Emit shipment validation report (CSV) ─────────────────────────
        # One row per shipment with the columns declared in the report node spec
        shipment_validation_report = await workflow.execute_activity(
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
                rows=[
                    {
                        "shipment_number":    shipment_number,
                        "invoices_processed": invoices_processed,
                        "invoices_succeeded": invoices_succeeded,
                        "invoices_failed":    invoices_failed,
                        "goods_failed":       goods_failed_total,
                        "batches_processed":  batches_processed,
                        "batches_succeeded":  batches_succeeded,
                        "batches_failed":     batches_failed,
                    }
                ],
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # ── Mark processed ─────────────────────────────────────────────────
        await workflow.execute_activity(
            mark_email_processed,
            EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
            start_to_close_timeout=_TIMEOUT,
        )

        return Board7AgentResult(
            message_id=inp.message_id,
            shipment_number=shipment_number,
            invoices_processed=invoices_processed,
            invoices_succeeded=invoices_succeeded,
            invoices_failed=invoices_failed,
            batches_processed=batches_processed,
            batches_succeeded=batches_succeeded,
            batches_failed=batches_failed,
            goods_failed=goods_failed_total,
            report_content=shipment_validation_report.content,
        )