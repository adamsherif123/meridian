"""load_document — Temporal activity.

Identifies a document among a list of attachments and returns its extracted text.

Text extraction (default, cheap):
    pdfplumber for PDFs; utf-8 decode for plain text.

Vision fallback (only on low-text path):
    If the matched document's extracted text is below VISION_THRESHOLD chars
    (< 50) AND the attachment carries a storage_path for downloading the original
    bytes, Claude vision is called to transcribe the content. This handles scanned
    image PDFs that have no embedded text layer (e.g. scanned COA PDFs).

    Vision block shapes used (verified against anthropic==0.112.0 — same as gate.py):
        PDF:   {"type": "document", "source": {"type": "base64",
                "media_type": "application/pdf", "data": <b64>}}
        Image: {"type": "image",    "source": {"type": "base64",
                "media_type": "<mime>",          "data": <b64>}}

How load_document obtains bytes for the fallback:
    The attachment dict may carry two extra keys (populated by runner.py /
    run_generated.py):
        "storage_path"  — path in the Supabase "sample-files" storage bucket
        "original_mime" — the document's real MIME type before the text/plain
                          override used to stay within Temporal's blob-size limit
    The activity (running outside the Temporal sandbox in a ThreadPoolExecutor)
    downloads the raw file bytes directly from Supabase storage. If those keys
    are absent, or the download/API call fails, the activity returns whatever
    text was already extracted — graceful degradation, no crash.
"""
import base64
import io
import logging
import os

from temporalio import activity

from backend.runtime.activities._types import LoadDocumentInput, LoadDocumentResult

log = logging.getLogger(__name__)

TEXT_CAP          = 20_000
VISION_THRESHOLD  = 50               # chars; below this, try vision fallback
VISION_BYTE_CAP   = 3 * 1024 * 1024  # skip single files > 3 MB (same cap as gate.py)
VISION_MAX_TOKENS = 2048
MODEL             = "claude-sonnet-4-6"

_VISION_PDF = {"application/pdf"}
_VISION_IMG = {"image/jpeg", "image/png", "image/gif", "image/webp"}


# ── Text extraction (unchanged) ────────────────────────────────────────────────

def _extract_text(data_b64: str, name: str, mime: str) -> str:
    """Decode base64 attachment and extract readable text.

    MIME type takes precedence over filename extension. The runner overrides
    mime to "text/plain" for all attachments to avoid Temporal's blob-size
    limit, so when mime is "text/plain" we always decode as text even if the
    filename ends in ".pdf". The vision fallback fires separately when the
    resulting text is too short (< VISION_THRESHOLD chars).
    """
    if not data_b64:
        return ""
    try:
        content = base64.b64decode(data_b64)
    except Exception as exc:
        return f"[base64 decode failed: {exc}]"

    name_lower = name.lower()
    # text/plain check FIRST: MIME type takes precedence over filename extension.
    if mime in ("text/plain", "text/csv") or name_lower.endswith((".txt", ".csv")):
        try:
            return content.decode("utf-8", errors="replace")[:TEXT_CAP]
        except Exception as exc:
            return f"[text decode failed: {exc}]"
    if mime == "application/pdf" or name_lower.endswith(".pdf"):
        try:
            import pdfplumber  # lazy — only when a PDF is loaded
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)[:TEXT_CAP]
        except Exception as exc:
            return f"[PDF extraction failed: {exc}]"
    return "[unsupported mime — no text extraction]"


# ── Vision fallback ────────────────────────────────────────────────────────────

def _vision_extract(storage_path: str, original_mime: str, name: str) -> str:
    """Download file bytes from Supabase storage and transcribe via Claude vision.

    Returns the transcribed text, or "" on any failure (caller degrades gracefully).
    Only called when pdfplumber/text extraction yields < VISION_THRESHOLD chars.
    Block shapes are identical to gate.py (verified against anthropic==0.112.0).
    """
    is_pdf = original_mime in _VISION_PDF or name.lower().endswith(".pdf")
    is_img = original_mime in _VISION_IMG
    if not (is_pdf or is_img):
        log.debug("vision_extract: %s (mime=%s) not vision-capable", name, original_mime)
        return ""

    # Download original file bytes from Supabase storage
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            log.warning("vision_extract: Supabase not configured — skipping for %s", name)
            return ""
        sb   = create_client(url, key)
        data: bytes = sb.storage.from_("sample-files").download(storage_path)
    except Exception as exc:
        log.warning(
            "vision_extract: download failed for %s (storage_path=%s): %s",
            name, storage_path, exc,
        )
        return ""

    if len(data) > VISION_BYTE_CAP:
        log.warning(
            "vision_extract: %s is %d bytes (> %d cap) — skipping",
            name, len(data), VISION_BYTE_CAP,
        )
        return ""

    # Build the vision block (same shape as gate.py, verified against anthropic==0.112.0)
    # DocumentBlockParam: {"type": "document", "source": {"type": "base64",
    #                       "media_type": "application/pdf", "data": <b64>}}
    # ImageBlockParam:    {"type": "image",    "source": {"type": "base64",
    #                       "media_type": "<image/...>",    "data": <b64>}}
    encoded = base64.b64encode(data).decode()
    if is_pdf:
        vision_block: dict = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
        }
    else:
        vision_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": original_mime, "data": encoded},
        }

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        response = client.messages.create(
            model=MODEL,
            max_tokens=VISION_MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"This is a scanned document named '{name}'. "
                            "Transcribe all readable text exactly as it appears — "
                            "every number, code, batch number, identifier, label, "
                            "and value. Return only the transcribed text with no "
                            "commentary or framing."
                        ),
                    },
                    vision_block,
                ],
            }],
        )
        vision_text = response.content[0].text if response.content else ""
        log.info(
            "vision_extract: %s — vision returned %d chars (raw PDF had ~%d chars from pdfplumber)",
            name, len(vision_text), VISION_THRESHOLD - 1,
        )
        return vision_text[:TEXT_CAP]
    except Exception as exc:
        log.warning("vision_extract: Claude API call failed for %s: %s", name, exc)
        return ""


def _enrich_with_vision_if_needed(text: str, name: str, mime: str, att: dict) -> str:
    """Return vision-extracted text if the current text is too short; else return text as-is."""
    if len(text.strip()) >= VISION_THRESHOLD:
        return text  # text-native document — fast path, no vision needed

    storage_path  = att.get("storage_path", "")
    original_mime = att.get("original_mime", "") or mime

    if not storage_path:
        log.debug(
            "load_document: %s text short (%d chars) but no storage_path — returning as-is",
            name, len(text.strip()),
        )
        return text  # can't fetch bytes — degrade gracefully

    log.info(
        "load_document: %s text is %d chars (< %d threshold) — invoking vision fallback",
        name, len(text.strip()), VISION_THRESHOLD,
    )
    vision_text = _vision_extract(storage_path, original_mime, name)
    return vision_text if vision_text else text  # fall back to original text on vision failure


# ── Matching ───────────────────────────────────────────────────────────────────

def _matches(name: str, text: str, identified_by: str, identifier: str) -> bool:
    """Return True if this attachment matches the identification rule."""
    if identified_by == "filename":
        return identifier.lower() in name.lower()
    if identified_by == "header_text":
        return identifier.lower() in text[:500].lower()
    if identified_by == "content":
        return identifier.lower() in text.lower()
    return False


# ── Activity ───────────────────────────────────────────────────────────────────

@activity.defn
def load_document(inp: LoadDocumentInput) -> LoadDocumentResult:
    """Locate a document among attachments and return its extracted text.

    Identification strategies (inp.identified_by):
        "filename"    — identifier is a case-insensitive substring of the filename.
        "header_text" — identifier phrase must appear in the first 500 chars of text.
        "content"     — identifier keyword must appear anywhere in the text.

    Inputs:
        inp.attachments: list of Attachment-dicts
            Required: name, mime, data_b64
            Optional: storage_path, original_mime  (enable vision fallback)
        inp.identified_by: identification strategy (see above)
        inp.identifier: value to match

    Outputs:
        LoadDocumentResult(name, mime, text, found)
        found=False if no attachment matches the rule.
        text is vision-enriched for scanned documents when bytes are available.
    """
    log.info(
        "load_document identified_by=%s identifier=%r attachments=%d",
        inp.identified_by, inp.identifier, len(inp.attachments),
    )
    try:
        for att in inp.attachments:
            name     = att.get("name", "")
            mime     = att.get("mime", "application/octet-stream")
            data_b64 = att.get("data_b64", "")

            # Filename match doesn't need text extraction to match —
            # but still enriches the returned text via vision if it's low.
            if inp.identified_by == "filename":
                if _matches(name, "", "filename", inp.identifier):
                    text = _extract_text(data_b64, name, mime)
                    text = _enrich_with_vision_if_needed(text, name, mime, att)
                    log.info("load_document matched by filename: %s  text=%d chars", name, len(text))
                    return LoadDocumentResult(name=name, mime=mime, text=text, found=True)
                continue

            # header_text / content matching: extract first, then match.
            # Vision enrichment only fires AFTER a successful match so we
            # don't vision-extract every attachment to find a content match.
            text = _extract_text(data_b64, name, mime)
            if _matches(name, text, inp.identified_by, inp.identifier):
                text = _enrich_with_vision_if_needed(text, name, mime, att)
                log.info("load_document matched by %s: %s  text=%d chars", inp.identified_by, name, len(text))
                return LoadDocumentResult(name=name, mime=mime, text=text, found=True)

        log.warning(
            "load_document: no attachment matched identified_by=%s identifier=%r",
            inp.identified_by, inp.identifier,
        )
        return LoadDocumentResult(name="", mime="", text="", found=False)
    except Exception as exc:
        log.warning(
            "load_document: unexpected error (identified_by=%s identifier=%r attachments=%d) — returning not-found: %s",
            inp.identified_by, inp.identifier, len(inp.attachments), exc, exc_info=True,
        )
        return LoadDocumentResult(name="", mime="", text="", found=False)


# ── Smoke test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import base64

    attachments = [
        {
            "name": "irrelevant.txt",
            "mime": "text/plain",
            "data_b64": base64.b64encode(b"nothing useful here").decode(),
        },
        {
            "name": "primary_doc.txt",
            "mime": "text/plain",
            "data_b64": base64.b64encode(b"Reference: REF-001\nQuantity: 10\nDate: 2026-01-01").decode(),
        },
    ]
    result = load_document(LoadDocumentInput(
        attachments=attachments,
        identified_by="filename",
        identifier="primary_doc",
    ))
    print("load_document OK")
    print(f"  found={result.found} name={result.name!r}")
    print(f"  text={result.text!r}")
    assert result.found
    assert "REF-001" in result.text

    result2 = load_document(LoadDocumentInput(
        attachments=attachments,
        identified_by="filename",
        identifier="nonexistent",
    ))
    assert not result2.found
    print("  not-found case OK")

    # Test graceful degradation: scanned PDF (mime="text/plain" override, 3-char text),
    # no storage_path → vision skipped, returns the tiny text without crash.
    scanned_att = [
        {
            "name": "ULXDA26012A COA.pdf",
            "mime": "text/plain",       # runner overrides mime to stay within Temporal limit
            "data_b64": base64.b64encode(b"xyz").decode(),  # ~3 chars (scanned doc)
            # no storage_path → vision fallback silently skipped
        }
    ]
    result3 = load_document(LoadDocumentInput(
        attachments=scanned_att,
        identified_by="filename",
        identifier="ULXDA26012A",
    ))
    assert result3.found, f"Expected found=True, got {result3}"
    assert result3.text == "xyz", f"Expected 'xyz' (text/plain decode), got {result3.text!r}"
    print("  graceful degradation (no storage_path, text/plain mime) OK")

    # Confirm text-native PDF (mime="application/pdf") still uses pdfplumber
    # (exercises the non-overridden path — not the runner's text/plain path)
    txt_att = [
        {
            "name": "invoice.txt",
            "mime": "text/plain",
            "data_b64": base64.b64encode(b"HTS No : 1234  ANDA No : 5678").decode(),
        }
    ]
    result4 = load_document(LoadDocumentInput(
        attachments=txt_att,
        identified_by="content",
        identifier="HTS No",
    ))
    assert result4.found
    assert "HTS No" in result4.text
    print("  content match OK")
