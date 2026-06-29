"""Shared dataclasses for all agent runtime activities.

These are the typed contracts that S8 codegen generates against.
All fields are JSON-serializable (no raw bytes). Binary content is
base64-encoded in data_b64 strings.

Collections of typed items (attachments, fields, items) use list[dict]
so Temporal's default JSON converter handles them safely without
needing to resolve generic-arg types at deserialization time.
Helper dataclasses (FieldSpec, CountKey, etc.) are used internally
within activities to provide typed access.
"""
from dataclasses import dataclass, field


# ── Attachment ─────────────────────────────────────────────────────────────────

@dataclass
class Attachment:
    """A single email attachment. data_b64 = base64-encoded content."""
    name: str
    mime: str
    data_b64: str  # base64; empty string = content not available


# ── Fetch email ────────────────────────────────────────────────────────────────

@dataclass
class FetchEmailInput:
    """
    S7 fixture mode: use_fixture=True, populate fixture_* fields.
    S11 live mode:   use_fixture=False, provide message_id — Composio Gmail called.
    """
    message_id: str = "fixture-001"
    use_fixture: bool = True
    fixture_subject: str = ""
    fixture_sender: str = ""
    fixture_body: str = ""
    fixture_attachments: list[dict] = field(default_factory=list)  # list[Attachment-dict]


@dataclass
class FetchEmailResult:
    message_id: str
    subject: str
    sender: str
    body_text: str
    attachments: list[dict]  # list[Attachment-dict]


# ── Load document ──────────────────────────────────────────────────────────────

@dataclass
class LoadDocumentInput:
    """Locate one document among a list of attachments by identification rule."""
    attachments: list[dict]   # list[Attachment-dict]
    identified_by: str        # "filename" | "header_text" | "content"
    identifier: str           # substring / phrase / keyword to match


@dataclass
class LoadDocumentResult:
    name: str
    mime: str
    text: str    # extracted text (empty if extraction failed / not applicable)
    found: bool


# ── Validate required fields ───────────────────────────────────────────────────

@dataclass
class FieldSpec:
    """Internal helper — created from list[dict] received by the activity."""
    name: str
    appears_as: str = ""     # recognition alias; empty = use name directly
    scope: str = "document"  # "document" | "line_item"
    required: bool = True


@dataclass
class FieldResult:
    name: str
    found: bool
    scope: str


@dataclass
class ValidateFieldsInput:
    document_text: str
    fields: list[dict]       # list[FieldSpec-dict]: {name, appears_as?, scope?, required?}
    fail_if: str             # "any_missing" | "all_missing" | "custom"
    applies_to: str = "per_document"  # "per_document" | "per_line_item"


@dataclass
class ValidateFieldsResult:
    passed: bool
    field_results: list[dict]  # list[FieldResult-dict]: {name, found, scope}
    fail_reason: str = ""


# ── Match documents ────────────────────────────────────────────────────────────

@dataclass
class MatchInput:
    source_items: list[dict]  # each dict must contain key_field
    target_items: list[dict]  # each dict must contain key_field
    key_field: str
    on_missing: str           # "fail" | "flag" | "ignore"
    match_type: str           # "exact" | "normalized" | "fuzzy"


@dataclass
class MatchResult:
    matched: list[dict]           # list[{source: dict, target: dict}]
    unmatched_source: list[dict]
    unmatched_target: list[dict]
    has_failures: bool


# ── Tally ──────────────────────────────────────────────────────────────────────

@dataclass
class CountKey:
    """Internal helper — created from list[dict] received by the activity."""
    collection: str   # key in result dicts that identifies which collection this belongs to
    dedup_key: str    # key to deduplicate on (same value = same item, counted once)
    label: str        # label for the output counter group
    track: list[str]  # which states to count: ["processed", "succeeded", "failed"]


@dataclass
class TallyInput:
    results: list[dict]      # raw result dicts from prior activities
    count_keys: list[dict]   # list[CountKey-dict]: {collection, dedup_key, label, track}


@dataclass
class TallyResult:
    counts: dict  # label → {state: int, ...}


# ── Report ─────────────────────────────────────────────────────────────────────

@dataclass
class EmitReportInput:
    format: str          # "csv" | "json"
    columns: list[str]
    rows: list[dict]     # each dict is one row; keys are column names


@dataclass
class EmitReportResult:
    content: str         # full CSV string or JSON string
    format: str
    row_count: int


@dataclass
class SendReportInput:
    report_content: str
    format: str
    recipient: str
    subject: str
    body: str
    attachment_filename: str = ""  # if set, upload content as this filename and attach it


@dataclass
class SendReportResult:
    sent: bool
    detail: str


# ── Classify documents ─────────────────────────────────────────────────────────

@dataclass
class ClassifyDocumentsInput:
    """Classify all attachments by content (LLM) into spec-defined document types.

    attachments: same Attachment-dict list from FetchEmailResult (with optional
        storage_path / original_mime keys added by the runner for vision fallback).
    doc_types: list of {type_name: str, description: str} dicts derived from the
        spec's expected_document nodes. Each description tells the LLM what content
        markers identify that document type. Always include an "other" entry.
    """
    attachments: list[dict]   # list of Attachment-dicts
    doc_types: list[dict]     # [{type_name: str, description: str}, ...]


@dataclass
class ClassifyDocumentsResult:
    """Classification result for every attachment.

    Each element of documents is a dict with keys:
        name       str  — attachment filename
        doc_type   str  — matched type_name from doc_types, or "other"
        identifier str  — primary key extracted from content (invoice number,
                          batch number, etc.); empty string if not found
        text       str  — full extracted text (vision-enriched for scanned PDFs);
                          pass directly to validate_required_fields or field parsers
    """
    documents: list[dict]


# ── Extract email facts ────────────────────────────────────────────────────────

@dataclass
class ExtractEmailFactsInput:
    """Extract structured shipment facts from the email subject + body using LLM.

    subject:          Email subject line.
    body:             Email body text.
    shipment_id_hint: Description of the shipment identifier format derived from the spec's
                      key_field or resolved assumptions — e.g. "MAWB number, format NNN-NNNNNNNN"
                      or "container number". Guides the LLM; use "" if unknown.
    invoice_hint:     Description of how invoice references appear in this sender's emails,
                      derived from spec's resolved assumptions — e.g. "invoice numbers after
                      'INVOICE NO' label, comma-separated, may be abbreviated". Use "" if invoice
                      numbers are not expected in the email header.
    """
    subject: str = ""
    body: str = ""
    shipment_id_hint: str = ""
    invoice_hint: str = ""


@dataclass
class ExtractEmailFactsResult:
    """Structured shipment facts extracted from the email.

    shipment_id:      Canonical shipment identifier (MAWB, container, BL number), or "".
    invoice_numbers:  Trailing numeric portions of each invoice reference, normalized and
                      expanded from abbreviated comma-separated forms. [] if none found.
    """
    shipment_id: str = ""
    invoice_numbers: list[str] = field(default_factory=list)


# ── Email dedup ────────────────────────────────────────────────────────────────

@dataclass
class EmailDedupInput:
    message_id: str
    board_id: str = ""   # optional — scope dedup per board; empty = global


@dataclass
class EmailDedupResult:
    already_processed: bool
    marked_at: str = ""  # ISO timestamp when the message was first processed
