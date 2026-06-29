"""classify_documents — Temporal activity.

Classifies every email attachment by document CONTENT (not filename) using Claude LLM,
and extracts the primary identifier from each (invoice number, batch number, etc.).
The document-type definitions come from the spec — this activity is fully general.

Text-extraction modes:
  Fixture mode: runner overrides mime → "text/plain"; storage_path set in att dict.
      Short-text PDFs trigger vision via Supabase storage download.
  Live mode:   mime = real MIME ("application/pdf"); no storage_path.
      Short-text PDFs trigger vision via raw bytes already in data_b64.

On any LLM failure the activity falls back to filename-keyword heuristics —
it never raises; it always returns a classification for every attachment.
"""
import base64
import concurrent.futures
import io
import json
import logging
import os
import re

from temporalio import activity

from backend.runtime.activities._types import (
    ClassifyDocumentsInput,
    ClassifyDocumentsResult,
)

log = logging.getLogger(__name__)

MODEL             = "claude-sonnet-4-6"
VISION_THRESHOLD  = 50               # chars below which vision fallback fires
VISION_BYTE_CAP   = 3 * 1024 * 1024  # 3 MB per file max for vision
TEXT_CAP_FULL     = 20_000           # chars stored in result.text
TEXT_CAP_CLASSIFY = 5_000            # chars sent to classifier LLM (large enough to reach identifiers in vision-transcribed docs)
CLASSIFY_TOKENS   = 4_096            # max_tokens for classification call (headroom for 20+ attachment sets)
VISION_TOKENS     = 2_048            # max_tokens for vision transcription call
VISION_WORKERS    = 6                # concurrent vision threads; caps simultaneous Anthropic API calls

_VISION_PDF = {"application/pdf"}
_VISION_IMG = {"image/jpeg", "image/png", "image/gif", "image/webp"}


# ── Text extraction ────────────────────────────────────────────────────────────

def _decode_text(data_b64: str, name: str, mime: str) -> str:
    """Decode base64 attachment bytes to readable text.

    When mime has been overridden to text/plain by the runner (fixture mode),
    data_b64 encodes the already-extracted text string — decode as UTF-8.
    Otherwise try pdfplumber for PDFs.
    """
    if not data_b64:
        return ""
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return ""

    name_lower = name.lower()
    if mime in ("text/plain", "text/csv") or name_lower.endswith((".txt", ".csv")):
        return raw.decode("utf-8", errors="replace")[:TEXT_CAP_FULL]

    if mime == "application/pdf" or name_lower.endswith(".pdf"):
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)[:TEXT_CAP_FULL]
        except Exception:
            return ""

    return ""


def _vision_api_call(vision_block: dict, name: str) -> str:
    """Call Claude vision to transcribe a scanned document block. Returns text or ""."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        response = client.messages.create(
            model=MODEL,
            max_tokens=VISION_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"This is a scanned document named '{name}'. "
                            "Transcribe all readable text exactly as it appears — "
                            "every number, code, batch number, identifier, label, "
                            "and value. Return only the transcribed text."
                        ),
                    },
                    vision_block,
                ],
            }],
        )
        text = response.content[0].text if response.content else ""
        log.info("classify: vision transcribed %s → %d chars", name, len(text))
        return text[:TEXT_CAP_FULL]
    except Exception as exc:
        log.warning("classify: vision API call failed for %s: %s", name, exc)
        return ""


def _vision_from_storage(storage_path: str, original_mime: str, name: str) -> str:
    """Fixture mode: download from Supabase storage and vision-extract."""
    is_pdf = original_mime in _VISION_PDF or name.lower().endswith(".pdf")
    is_img = original_mime in _VISION_IMG
    if not (is_pdf or is_img):
        return ""

    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            log.warning("classify: Supabase not configured — vision skipped for %s", name)
            return ""
        sb   = create_client(url, key)
        data = sb.storage.from_("sample-files").download(storage_path)
    except Exception as exc:
        log.warning("classify: storage download failed for %s: %s", name, exc)
        return ""

    if len(data) > VISION_BYTE_CAP:
        log.warning("classify: %s too large (%d B) for vision", name, len(data))
        return ""

    encoded = base64.b64encode(data).decode()
    if is_pdf:
        block: dict = {"type": "document",
                       "source": {"type": "base64", "media_type": "application/pdf", "data": encoded}}
    else:
        block = {"type": "image",
                 "source": {"type": "base64", "media_type": original_mime, "data": encoded}}
    return _vision_api_call(block, name)


def _vision_from_b64(data_b64: str, original_mime: str, name: str) -> str:
    """Live mode: the raw PDF/image bytes are already in data_b64."""
    is_pdf = original_mime in _VISION_PDF or name.lower().endswith(".pdf")
    is_img = original_mime in _VISION_IMG
    if not (is_pdf or is_img):
        return ""

    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return ""

    if len(raw) > VISION_BYTE_CAP:
        log.warning("classify: %s too large (%d B) for vision", name, len(raw))
        return ""

    if is_pdf:
        block: dict = {"type": "document",
                       "source": {"type": "base64", "media_type": "application/pdf", "data": data_b64}}
    else:
        block = {"type": "image",
                 "source": {"type": "base64", "media_type": original_mime, "data": data_b64}}
    return _vision_api_call(block, name)


def _get_text(att: dict) -> str:
    """Full text extraction with vision fallback for scanned PDFs.

    Fixture mode: mime=text/plain (runner override), storage_path present
                  → vision reads raw bytes from Supabase.
    Live mode:    mime=application/pdf, no storage_path
                  → vision reads from data_b64 bytes already in the attachment.
    """
    name         = att.get("name", "")
    mime         = att.get("mime", "")
    data_b64     = att.get("data_b64", "")
    storage_path = att.get("storage_path", "")
    original_mime = att.get("original_mime", "") or mime

    text = _decode_text(data_b64, name, mime)

    if len(text.strip()) < VISION_THRESHOLD:
        is_vision_capable = (
            original_mime in _VISION_PDF | _VISION_IMG
            or name.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"))
        )
        if is_vision_capable:
            if storage_path:
                # Fixture mode: download from Supabase storage
                vision_text = _vision_from_storage(storage_path, original_mime, name)
            elif mime not in ("text/plain", "text/csv"):
                # Live mode: data_b64 holds raw file bytes
                vision_text = _vision_from_b64(data_b64, original_mime or mime, name)
            else:
                vision_text = ""
            if vision_text:
                return vision_text

    return text


# ── LLM classification ─────────────────────────────────────────────────────────

def _classify_via_llm(
    docs: list[dict],           # [{name, text}, ...]
    doc_types: list[dict],      # [{type_name, description}, ...]
) -> list[dict]:
    """Call Claude to classify docs. Returns [{name, type, identifier}, ...]."""
    type_lines = "\n".join(
        f'  "{dt["type_name"]}": {dt["description"]}'
        for dt in doc_types
    )
    if not any(dt.get("type_name") == "other" for dt in doc_types):
        type_lines += '\n  "other": does not match any of the above types'

    doc_entries = []
    for i, d in enumerate(docs, 1):
        preview = d["text"][:TEXT_CAP_CLASSIFY]
        doc_entries.append(f"### [{i}] filename: {d['name']!r}\n{preview}\n")

    prompt = f"""Classify each document by its text CONTENT. Return a JSON array ONLY — no prose, no markdown fences.

CLASSIFICATION RULES:
- Classify based STRICTLY on the document text shown below. The filename is provided only for reference and identifier extraction — do NOT use it to determine document type.
- Assign a type ONLY when the document text CLEARLY contains the content markers described for that type. If required markers are absent or the text is too short/empty to confirm, classify as "other".
- When two types seem plausible, prefer the more specific type only if its required markers are unambiguously present in the text; otherwise classify as "other".

DOCUMENT TYPES (use exactly the quoted type_name string):
{type_lines}

For each document determine:
1. "type": the type_name whose described content markers are clearly present in the document text. Use "other" when uncertain or when the required markers are absent.
2. "identifier": the primary key extracted from the text (NOT from the filename):
   - commercial invoice → the invoice or bill number
   - certificate of analysis → the batch number or lot number (e.g. the value after "Alias Batch No." or "Lot No.")
   - packing list → the invoice or shipment reference it covers
   - other document types → any primary reference number
   - use "" when no clear identifier is present in the text

Return a JSON array with one object per document, SAME ORDER as input:
[
  {{"name": "<filename>", "type": "<type_name>", "identifier": "<key or empty>"}},
  ...
]

DOCUMENTS:

{"".join(doc_entries)}"""

    try:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=CLASSIFY_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (response.content[0].text if response.content else "[]").strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw.strip())
    except Exception as exc:
        log.warning("classify: LLM classification failed: %s", exc)
        return []


# ── Filename heuristic fallback ────────────────────────────────────────────────

def _filename_fallback(name: str, text: str, doc_types: list[dict]) -> str:
    """Guess doc type from filename keywords when LLM is unavailable.

    Checks type_name tokens and description keywords against both the filename
    and a short prefix of the text.
    """
    name_lower = name.lower()
    text_head  = text[:500].lower()

    for dt in doc_types:
        type_name = dt.get("type_name", "")
        desc      = dt.get("description", "").lower()
        if type_name == "other":
            continue
        candidates = set(type_name.replace("_", " ").split())
        # Pull strong keywords from the description (4+ char words)
        desc_words = {w.strip(".,':\"()") for w in desc.split() if len(w) >= 4}
        candidates |= desc_words

        for kw in candidates:
            if len(kw) < 4:
                continue
            if kw in name_lower or kw in text_head:
                return type_name

    return "other"


# ── Activity ───────────────────────────────────────────────────────────────────

@activity.defn
def classify_documents(inp: ClassifyDocumentsInput) -> ClassifyDocumentsResult:
    """Classify every attachment by content into spec-defined document types.

    Returns a ClassifyDocumentsResult whose .documents list carries one entry per
    attachment:
        {"name": str, "doc_type": str, "identifier": str, "text": str}

    The "text" field is vision-enriched for scanned PDFs and is ready for
    direct use in validate_required_fields or content parsers — no separate
    load_document call is needed for documents processed here.

    Falls back to filename-based heuristics on any LLM failure.
    Never raises; always returns a full classification.
    """
    log.info(
        "classify_documents: %d attachments, types=%s",
        len(inp.attachments),
        [dt.get("type_name") for dt in inp.doc_types],
    )

    # ── Step 1: extract text for every attachment (vision for scanned PDFs) ───
    # Vision calls are I/O-bound and independent — run them in parallel so that
    # N scanned PDFs take ~1 round-trip time instead of N × 40s serially.
    def _extract_one(att: dict) -> dict:
        name = att.get("name", "")
        text = _get_text(att)
        log.info("classify_documents: extracted %s → %d chars", name, len(text))
        return {"name": name, "text": text}

    with concurrent.futures.ThreadPoolExecutor(max_workers=VISION_WORKERS) as pool:
        # map() preserves submission order — docs_with_text matches inp.attachments order.
        docs_with_text = list(pool.map(_extract_one, inp.attachments))

    # ── Step 2: LLM classification ─────────────────────────────────────────────
    try:
        llm_results = _classify_via_llm(docs_with_text, inp.doc_types)
        llm_map = {r.get("name", ""): r for r in llm_results}
        log.info("classify_documents: LLM returned %d classifications", len(llm_results))
    except Exception as exc:
        log.warning("classify_documents: LLM step failed (%s) — all will use fallback", exc)
        llm_map = {}

    # ── Step 3: assemble result (fallback for any gap) ─────────────────────────
    documents: list[dict] = []
    for d in docs_with_text:
        name = d["name"]
        text = d["text"]
        llm = llm_map.get(name)

        if llm and llm.get("type"):
            doc_type   = llm["type"]
            identifier = llm.get("identifier", "")
        else:
            doc_type   = _filename_fallback(name, text, inp.doc_types)
            identifier = ""
            if llm_map:
                log.warning(
                    "classify_documents: no LLM result for %r — using filename fallback → %s",
                    name, doc_type,
                )

        documents.append({
            "name":       name,
            "doc_type":   doc_type,
            "identifier": identifier,
            "text":       text,
        })
        log.info(
            "classify_documents: %s → doc_type=%s identifier=%r",
            name, doc_type, identifier,
        )

    return ClassifyDocumentsResult(documents=documents)
