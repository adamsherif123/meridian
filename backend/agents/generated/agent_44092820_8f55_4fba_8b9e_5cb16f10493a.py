"""Pre-alert pharmaceutical shipment document verification workflow.

Generated from spec: Board 9
Frozen at: 2026-06-28T22:03:20.297248+00:00
DO NOT EDIT MANUALLY — regenerate via POST /api/v1/boards/44092820-8f55-4fba-8b9e-5cb16f10493a/codegen
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

# Invoice filename pattern: U06-NNN.pdf (digits before .pdf, excludes -PL packing-list variants)
_INVOICE_RE = _re.compile(r'U06-\d+\.pdf$', _re.IGNORECASE)

# COA filename pattern: <BATCH> COA.pdf
_COA_RE = _re.compile(r'COA\.pdf$', _re.IGNORECASE)

# Token regex for extracting batch numbers from BATCH NOS: lines
# Batch numbers are alphanumeric identifiers with at least one digit (e.g. ULXDA26010A)
_BATCH_TOKEN_RE = _re.compile(r'\b([A-Za-z][A-Za-z0-9]{6,})\b')

# Invoice number extraction from subject: "INV NO: U06/26-27/335,336 & 353" → [335, 336, 353]
_INV_SUBJECT_RE = _re.compile(r'U06[/\-][\d\-/]+[/\-](\d[\d,&\s]+)', _re.IGNORECASE)
_INV_NUM_RE = _re.compile(r'\d{3,}')

# Alias Batch No. extraction from COA text (P5 pattern)
_ALIAS_BATCH_RE = _re.compile(
    r'Alias\s+Batch\s+No\.?'           # label with optional trailing period
    r'[\s|:.]*'                          # any separators or none
    r'([A-Za-z]{2,}\d+[A-Za-z])',       # key token: 2+ letters, digits, trailing letter
    _re.IGNORECASE,
)


@dataclass
class Board9AgentInput:
    message_id: str = ""
    use_fixture: bool = True
    fixture_body: str = ""
    fixture_attachments: list[dict] = field(default_factory=list)
    board_id: str = ""
    fixture_subject: str = ""   # eval/fixture runs set the shipment key via this field
    fixture_sender: str = ""


@dataclass
class Board9AgentResult:
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
    skipped: bool = False


@workflow.defn
class Board9AgentWorkflow:
    @workflow.run
    async def run(self, inp: Board9AgentInput) -> Board9AgentResult:
        # ── Dedup guard ────────────────────────────────────────────────────
        dedup = await workflow.execute_activity(
            is_email_processed,
            EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
            start_to_close_timeout=_TIMEOUT,
        )
        if dedup.already_processed:
            return Board9AgentResult(
                message_id=inp.message_id,
                skipped=True,
            )

        # ── Fetch email and attachments ────────────────────────────────────
        # Resolves: trigger node + Read/Fetch data tool_action node
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

        # ── Extract shipment number (MAWB/container) from subject (P4) ────
        # Prefer subject directly; it contains the shipment key
        shipment_number = fetch_result.subject.strip()

        # ── Extract invoice numbers from subject and map to attachments (resolved assumption) ──
        # Subject format example: "INV NO: U06/26-27/335,336 & 353"
        # Parse invoice numbers (3-digit suffixes) then map to U06-NNN.pdf attachments
        invoice_numbers: list[str] = []
        subject_upper = fetch_result.subject.upper()
        inv_match = _INV_SUBJECT_RE.search(fetch_result.subject)
        if inv_match:
            num_portion = inv_match.group(1)
            invoice_numbers = _INV_NUM_RE.findall(num_portion)
        else:
            # Fallback: scan body for invoice numbers
            body_match = _INV_SUBJECT_RE.search(fetch_result.body_text)
            if body_match:
                invoice_numbers = _INV_NUM_RE.findall(body_match.group(1))

        # Build the "Invoices in the shipment" collection:
        # For each invoice number found, locate the corresponding U06-NNN.pdf attachment.
        # Per P3: positive match using _INVOICE_RE (excludes -PL packing-list variants).
        invoice_attachments: list[dict] = []
        seen_invoice_names: set[str] = set()
        for att in fetch_result.attachments:
            att_name = att.get("name", "")
            if _INVOICE_RE.search(att_name) and att_name not in seen_invoice_names:
                # Optionally restrict to only invoice numbers we parsed
                # (robust: include all U06-NNN.pdf found; they are the invoice documents)
                seen_invoice_names.add(att_name)
                invoice_attachments.append(att)

        # If subject parsing yielded invoice numbers, filter to only those specific invoices
        # (e.g. U06-335.pdf, U06-336.pdf, U06-353.pdf for invoice numbers 335, 336, 353)
        if invoice_numbers:
            filtered: list[dict] = []
            for att in invoice_attachments:
                att_name = att.get("name", "")
                for inv_num in invoice_numbers:
                    # Match U06-NNN.pdf where NNN is the invoice number suffix
                    if _re.search(r'U06-0*' + _re.escape(inv_num) + r'\.pdf$', att_name, _re.IGNORECASE):
                        filtered.append(att)
                        break
            if filtered:
                invoice_attachments = filtered

        # ── Pre-load all COA attachments concurrently (P6) ────────────────
        # COAs are named <BATCH> COA.pdf — identify them positively
        coa_attachments: list[dict] = []
        seen_coa_names: set[str] = set()
        for att in fetch_result.attachments:
            att_name = att.get("name", "")
            if _COA_RE.search(att_name) and att_name not in seen_coa_names:
                seen_coa_names.add(att_name)
                coa_attachments.append(att)

        # Load all COA documents concurrently — vision reads are expensive, parallelise
        _coa_cache: dict[str, object] = {}  # filename → LoadDocumentResult
        if coa_attachments:
            _coa_tasks = [
                workflow.execute_activity(
                    load_document,
                    LoadDocumentInput(
                        attachments=fetch_result.attachments,
                        identified_by="filename",
                        identifier=att.get("name", ""),
                    ),
                    start_to_close_timeout=_VISION_TIMEOUT,
                )
                for att in coa_attachments
            ]
            _coa_results = await asyncio.gather(*_coa_tasks)
            for att, res in zip(coa_attachments, _coa_results):
                _coa_cache[att.get("name", "")] = res

        # ── Collect all tally results across all invoices ──────────────────
        # Per resolved assumption: one row per shipment, aggregating all invoices
        all_tally_results: list[dict] = []

        # ── Outer loop: for each Invoice in Invoices in the shipment ──────
        for invoice_att in invoice_attachments:
            invoice_filename = invoice_att.get("name", "")

            # Load the commercial invoice document
            # identified_by="filename", identifier = full filename (P rule: use att.get("name"))
            invoice_doc = await workflow.execute_activity(
                load_document,
                LoadDocumentInput(
                    attachments=fetch_result.attachments,
                    identified_by="filename",
                    identifier=invoice_filename,
                ),
                start_to_close_timeout=_VISION_TIMEOUT,
            )

            invoice_text = invoice_doc.text if invoice_doc.found else ""

            # Extract invoice number from filename for tracking (e.g. "U06-335.pdf" → "U06-335")
            inv_num_match = _re.search(r'(U06-\d+)', invoice_filename, _re.IGNORECASE)
            invoice_number_key = inv_num_match.group(1) if inv_num_match else invoice_filename

            # ── Check a document: validate regulatory codes per line item ──
            # Per spec: HTS, ANDA, FDA, REG NO, NDC — all per line_item scope
            # Per resolved assumption: NDC appears as "NDC No :" in the invoice
            # Per P1: use whitespace-normalised text for validate_required_fields
            normalised_invoice_text = " ".join(invoice_text.split())

            check_a_document = await workflow.execute_activity(
                validate_required_fields,
                ValidateFieldsInput(
                    document_text=normalised_invoice_text,
                    fields=[
                        {"name": "HTS",    "appears_as": "HTS",    "scope": "line_item", "required": True},
                        {"name": "ANDA",   "appears_as": "ANDA",   "scope": "line_item", "required": True},
                        {"name": "FDA",    "appears_as": "FDA",    "scope": "line_item", "required": True},
                        {"name": "REG NO", "appears_as": "FEI",    "scope": "line_item", "required": True},
                        {"name": "NDC",    "appears_as": "NDC",    "scope": "line_item", "required": True},
                    ],
                    fail_if="any_missing",
                    applies_to="per_document",
                ),
                start_to_close_timeout=_TIMEOUT,
            )

            # Determine if this invoice's goods validation passed
            invoice_goods_failed = not check_a_document.passed

            # Record invoice-level result for tally
            invoice_result = {
                "invoices": invoice_number_key,          # collection key for tally
                "invoice number": invoice_number_key,    # dedup_key
                "passed": check_a_document.passed,
            }

            # ── Extract batch numbers from invoice (P2 pattern) ────────────
            # Batch numbers appear as "BATCH NOS:" lines in the invoice text, comma-separated
            # Per P2: scan both the text after the colon AND the next line, filter by digit
            invoice_batches: list[str] = []
            lines = invoice_text.splitlines()
            for i, line in enumerate(lines):
                if "BATCH NOS" in line.upper():
                    after = ""
                    colon_idx = line.find(":")
                    if colon_idx != -1:
                        after = line[colon_idx + 1:]
                    next_line = lines[i + 1] if i + 1 < len(lines) else ""
                    search_text = after + " " + next_line
                    for m in _BATCH_TOKEN_RE.finditer(search_text):
                        tok = m.group(1)
                        # Require at least one digit — rejects pure-word description tokens
                        if any(c.isdigit() for c in tok):
                            if tok not in invoice_batches:
                                invoice_batches.append(tok)

            # ── Inner loop: for each batch on the invoice ──────────────────
            for batch_number in invoice_batches:

                # ── Match batch to its COA document ───────────────────────
                # COA files are named "<BATCH> COA.pdf" — find the COA for this batch
                # First, try to locate the matching COA from the pre-loaded cache
                batch_coa_doc = None
                batch_coa_text = ""

                # Search cache for the COA that corresponds to this batch number
                # COA filename pattern: "<BATCH> COA.pdf" e.g. "ULXDA26010A COA.pdf"
                for coa_name, coa_doc_result in _coa_cache.items():
                    if batch_number.upper() in coa_name.upper():
                        batch_coa_doc = coa_doc_result
                        batch_coa_text = coa_doc_result.text if coa_doc_result.found else ""
                        break

                # If not found in cache (e.g. COA filename differs), try a direct load
                if batch_coa_doc is None:
                    # Try loading by content/filename with batch number as identifier
                    batch_coa_doc = await workflow.execute_activity(
                        load_document,
                        LoadDocumentInput(
                            attachments=fetch_result.attachments,
                            identified_by="filename",
                            identifier=batch_number,
                        ),
                        start_to_close_timeout=_VISION_TIMEOUT,
                    )
                    batch_coa_text = batch_coa_doc.text if batch_coa_doc.found else ""
                    _coa_cache[batch_number] = batch_coa_doc

                # Extract "Alias Batch No." from COA text (P5 pattern)
                # Per resolved assumption: COA field is "Alias Batch No." (e.g. ULXDA26012A)
                coa_alias_batch = ""
                _alias_m = _ALIAS_BATCH_RE.search(batch_coa_text)
                if _alias_m:
                    coa_alias_batch = _alias_m.group(1).strip()
                # P5 literal fallback: if the batch number appears literally in the COA text
                if not coa_alias_batch and batch_number and batch_number in batch_coa_text:
                    coa_alias_batch = batch_number

                # Build source and target item lists for match_by_key
                # Source: batch from invoice; target: COA with its alias batch number
                source_batch_item = {
                    "batch number": batch_number,
                    "invoice number": invoice_number_key,
                }
                target_coa_item = {
                    "batch number": coa_alias_batch,
                    "coa_filename": batch_coa_doc.name if batch_coa_doc else "",
                }

                # ── Match documents: invoice batch ↔ COA Alias Batch No. ──
                match_up_documents = await workflow.execute_activity(
                    match_by_key,
                    MatchInput(
                        source_items=[source_batch_item],
                        target_items=[target_coa_item],
                        key_field="batch number",
                        on_missing="fail",
                        match_type="exact",
                    ),
                    start_to_close_timeout=_TIMEOUT,
                )

                # Record batch-level result for tally
                batch_result = {
                    "batches": batch_number,             # collection key for tally
                    "batch number": batch_number,        # dedup_key
                    "passed": not match_up_documents.has_failures,
                    "invoice number": invoice_number_key,
                }
                all_tally_results.append(batch_result)

            # Append invoice-level result (after inner loop)
            # Also record goods-level result if validation failed
            all_tally_results.append(invoice_result)

            if invoice_goods_failed:
                # Track failed goods — one entry per line item (goods failed = invoice failed)
                # Per resolved assumption: goods tracked only for 'failed' track
                goods_result = {
                    "goods ": invoice_number_key,            # collection key for tally (matches spec: "goods ")
                    "good / line item ": invoice_number_key, # dedup_key (matches spec)
                    "passed": False,
                }
                all_tally_results.append(goods_result)

        # ── Tally results per shipment ─────────────────────────────────────
        # Three grains: invoices, batches, goods — per resolved assumption
        count_result = await workflow.execute_activity(
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
                        # Note: collection key has trailing space per spec definition
                        "collection": "goods ",
                        "dedup_key": "good / line item ",
                        "label": "goods",
                        "track": ["failed"],   # intentional: only failed tracked for goods
                    },
                ],
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # ── Extract tally counts for report row ───────────────────────────
        counts = count_result.counts
        invoices_counts  = counts.get("invoices", {})
        batches_counts   = counts.get("batches",  {})
        goods_counts     = counts.get("goods",    {})

        invoices_processed  = invoices_counts.get("processed",  0)
        invoices_succeeded  = invoices_counts.get("succeeded",  0)
        invoices_failed     = invoices_counts.get("failed",     0)
        batches_processed   = batches_counts.get("processed",   0)
        batches_succeeded   = batches_counts.get("succeeded",   0)
        batches_failed      = batches_counts.get("failed",      0)
        goods_failed        = goods_counts.get("failed",        0)

        # ── Emit report: one CSV row per shipment run (resolved assumption) ─
        report_row = {
            "shipment_number":    shipment_number,
            "invoices_processed": invoices_processed,
            "invoices_succeeded": invoices_succeeded,
            "invoices_failed":    invoices_failed,
            "goods_failed":       goods_failed,
            "batches_processed":  batches_processed,
            "batches_succeeded":  batches_succeeded,
            "batches_failed":     batches_failed,
        }

        report_results = await workflow.execute_activity(
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

        # ── Mark processed ────────────────────────────────────────────────
        await workflow.execute_activity(
            mark_email_processed,
            EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
            start_to_close_timeout=_TIMEOUT,
        )

        return Board9AgentResult(
            message_id=inp.message_id,
            shipment_number=shipment_number,
            invoices_processed=invoices_processed,
            invoices_succeeded=invoices_succeeded,
            invoices_failed=invoices_failed,
            batches_processed=batches_processed,
            batches_succeeded=batches_succeeded,
            batches_failed=batches_failed,
            goods_failed=goods_failed,
            report_content=report_results.content,
            skipped=False,
        )