"""fetch_email_and_attachments — Temporal activity.

Fixture mode (use_fixture=True):  returns provided data verbatim (eval / self-heal).
Live mode    (use_fixture=False): fetches a specific Gmail message + attachments via
                                   Composio and returns the same FetchEmailResult shape.

Live fetch flow (verified against composio==0.16.0 / composio-client==1.41.0):
    1. GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID(message_id=<gmail_hex_id>, format="full")
       → data.{subject, sender, messageText, attachmentList}
    2. For each attachment in attachmentList:
       GMAIL_GET_ATTACHMENT(message_id, attachment_id, file_name)
       → data.file.s3url  (pre-signed S3 URL — download via httpx)
    3. Extract text from bytes (pdfplumber for PDF, utf-8 for text/plain)
    4. Upload raw bytes to Supabase "sample-files" bucket at
       live-runs/{gmail_message_id}/{safe_name}  (enables vision fallback in load_document)
    5. Return FetchEmailResult with attachment dicts using the same shape as fixture
       mode: mime="text/plain", data_b64=base64(extracted_text), storage_path, original_mime

Required env vars for live mode:
    COMPOSIO_API_KEY             — Composio API key (already in .env)
    COMPOSIO_CONNECTED_ACCOUNT_ID — Gmail connected-account ID from Composio dashboard
    SUPABASE_URL / SUPABASE_SERVICE_KEY — already in .env (for storage upload)

Optional:
    ANTHROPIC_API_KEY — already in .env (needed only if vision fallback fires in load_document)

The activity runs in a ThreadPoolExecutor outside Temporal's sandbox — all I/O is allowed.
"""
import base64
import io
import logging
import os
import re

from temporalio import activity
from temporalio.exceptions import ApplicationError

from backend.runtime.activities._types import FetchEmailInput, FetchEmailResult

log = logging.getLogger(__name__)

TEXT_CAP = 20_000
BUCKET   = "sample-files"


# ── Text extraction from raw bytes ─────────────────────────────────────────────

def _extract_text_from_bytes(data: bytes, name: str, mime: str) -> str:
    """Extract readable text from raw attachment bytes.

    Mirrors load_document._extract_text but works on raw bytes (not base64).
    Returns "" on failure rather than an error string so the caller can decide.
    """
    name_lower = (name or "").lower()
    if mime in ("text/plain", "text/csv") or name_lower.endswith((".txt", ".csv")):
        try:
            return data.decode("utf-8", errors="replace")[:TEXT_CAP]
        except Exception as exc:
            log.warning("_extract_text_from_bytes: utf-8 decode failed for %s: %s", name, exc)
            return ""
    if mime == "application/pdf" or name_lower.endswith(".pdf"):
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)[:TEXT_CAP]
        except Exception as exc:
            log.warning("_extract_text_from_bytes: pdfplumber failed for %s: %s", name, exc)
            return ""
    return ""


# ── Supabase storage upload ────────────────────────────────────────────────────

def _upload_to_storage(data: bytes, gmail_message_id: str, filename: str, mime_type: str) -> str:
    """Upload raw attachment bytes to Supabase storage for the vision fallback.

    Returns the storage_path on success, "" on any failure (graceful degradation).
    Path: live-runs/{gmail_message_id}/{safe_filename}
    """
    if not data:
        return ""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return ""
    try:
        from supabase import create_client
        sb = create_client(url, key)
        safe_name = re.sub(r"[^a-zA-Z0-9._\- ]", "_", filename).strip() or "attachment"
        storage_path = f"live-runs/{gmail_message_id}/{safe_name}"
        sb.storage.from_(BUCKET).upload(
            path=storage_path,
            file=data,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
        log.debug("_upload_to_storage: uploaded %s (%d bytes)", storage_path, len(data))
        return storage_path
    except Exception as exc:
        log.warning("_upload_to_storage: failed for %s/%s: %s", gmail_message_id, filename, exc)
        return ""


# ── Live Gmail fetch (use_fixture=False path) ──────────────────────────────────

def _fetch_via_composio(gmail_message_id: str) -> FetchEmailResult:
    """Fetch a specific Gmail message + attachments via Composio.

    Args:
        gmail_message_id: Gmail API hex message ID (e.g. "19b11732c1b578fd").
                          Obtained from GMAIL_FETCH_EMAILS in the run-live endpoint;
                          passed in as inp.message_id.

    Verified call shapes (composio==0.16.0, composio-client==1.41.0):
        execute(slug="GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
                arguments={"message_id": <hex_id>, "format": "full", "user_id": "me"},
                connected_account_id=<id>)
        → response["data"]["subject" | "sender" | "messageText" | "attachmentList"]

        execute(slug="GMAIL_GET_ATTACHMENT",
                arguments={"message_id": <hex_id>, "attachment_id": <att_id>,
                           "file_name": <name>, "user_id": "me"},
                connected_account_id=<id>)
        → response["data"]["file"]["s3url"]  (pre-signed URL — download with httpx)
    """
    import httpx
    from composio import Composio

    api_key = os.environ.get("COMPOSIO_API_KEY", "")
    connected_account_id = os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "")
    composio_user_id = os.environ.get("COMPOSIO_USER_ID", "")

    if not api_key:
        raise ApplicationError("COMPOSIO_API_KEY not configured", non_retryable=True)
    if not connected_account_id:
        raise ApplicationError(
            "COMPOSIO_CONNECTED_ACCOUNT_ID not configured. "
            "Connect a Gmail account in Composio, then add the connected-account ID "
            "to backend/.env as COMPOSIO_CONNECTED_ACCOUNT_ID=<id>",
            non_retryable=True,
        )
    if not composio_user_id:
        raise ApplicationError(
            "COMPOSIO_USER_ID not configured. "
            "Set it to the entity/user ID the Gmail account was connected under "
            "(e.g. COMPOSIO_USER_ID=meridian-pharma) in backend/.env",
            non_retryable=True,
        )

    # Pin the Gmail toolkit version so Composio doesn't reject 'latest' in manual
    # (.execute) calls. Default: "20260626_00" (current latest as of 2026-06-28).
    # Override via env var COMPOSIO_GMAIL_TOOLKIT_VERSION if a newer version is needed.
    gmail_version = os.environ.get("COMPOSIO_GMAIL_TOOLKIT_VERSION", "20260626_00")
    composio = Composio(api_key=api_key, toolkit_versions={"gmail": gmail_version})

    # ── Step 1: Fetch the full message ────────────────────────────────────────
    log.info("_fetch_via_composio: fetching message_id=%s", gmail_message_id)
    try:
        msg_resp = composio.tools.execute(
            slug="GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
            arguments={"message_id": gmail_message_id, "format": "full", "user_id": "me"},
            connected_account_id=connected_account_id,
            user_id=composio_user_id,
        )
    except Exception as exc:
        raise ApplicationError(
            f"GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID failed: {exc}", non_retryable=False,
        ) from exc

    if not msg_resp.get("successful"):
        raise ApplicationError(
            f"GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID failed: {msg_resp.get('error', 'unknown error')}",
            non_retryable=False,
        )

    msg = msg_resp.get("data") or {}
    subject        = (msg.get("subject") or "").strip()
    sender         = (msg.get("sender")  or "").strip()
    body_text      = (msg.get("messageText") or "").strip()
    attachment_list = msg.get("attachmentList") or []

    log.info(
        "_fetch_via_composio: subject=%r sender=%r body=%d chars attachments=%d",
        subject, sender, len(body_text), len(attachment_list),
    )

    # ── Step 2: Download each attachment ──────────────────────────────────────
    attachments: list[dict] = []

    for att in attachment_list:
        # Attachment item key names — be defensive against field-name variants
        att_id   = (att.get("attachmentId") or att.get("attachment_id")
                    or att.get("id") or "")
        filename = (att.get("filename")     or att.get("fileName")
                    or att.get("name")      or "attachment")
        mime_type = (att.get("mimeType")   or att.get("mime_type")
                     or att.get("mimetype") or "application/octet-stream")

        if not att_id:
            log.warning(
                "_fetch_via_composio: attachment %r has no attachmentId — skipping", filename,
            )
            continue

        log.info("_fetch_via_composio: downloading attachment %s (mime=%s)", filename, mime_type)

        # Download via GMAIL_GET_ATTACHMENT → S3 pre-signed URL → httpx
        raw_bytes: bytes = b""
        try:
            att_resp = composio.tools.execute(
                slug="GMAIL_GET_ATTACHMENT",
                arguments={
                    "message_id":    gmail_message_id,
                    "attachment_id": att_id,
                    "file_name":     filename,
                    "user_id":       "me",
                },
                connected_account_id=connected_account_id,
                user_id=composio_user_id,
            )
            if att_resp.get("successful"):
                s3url = ((att_resp.get("data") or {}).get("file") or {}).get("s3url", "")
                if s3url:
                    r = httpx.get(s3url, timeout=60, follow_redirects=True)
                    r.raise_for_status()
                    raw_bytes = r.content
                    log.info(
                        "_fetch_via_composio: downloaded %s — %d bytes", filename, len(raw_bytes),
                    )
                else:
                    log.warning("_fetch_via_composio: no s3url in attachment response for %s", filename)
            else:
                log.warning(
                    "_fetch_via_composio: GMAIL_GET_ATTACHMENT failed for %s: %s",
                    filename, att_resp.get("error"),
                )
        except Exception as exc:
            log.warning("_fetch_via_composio: attachment download error for %s: %s", filename, exc)

        # Extract text from raw bytes (pdfplumber / utf-8 decode)
        extracted_text = _extract_text_from_bytes(raw_bytes, filename, mime_type)

        # Upload raw bytes to Supabase storage so load_document's vision fallback can
        # fetch the original bytes (same path as fixture files — "sample-files" bucket)
        storage_path = _upload_to_storage(raw_bytes, gmail_message_id, filename, mime_type)

        # Encode extracted text as base64 (text/plain override mirrors the fixture
        # runner pattern — avoids Temporal blob-size limits)
        data_b64 = (
            base64.b64encode(extracted_text.encode()).decode() if extracted_text else ""
        )

        attachments.append({
            "name":          filename,
            "mime":          "text/plain",    # overridden — see load_document._extract_text
            "data_b64":      data_b64,
            "storage_path":  storage_path,    # enables vision fallback in load_document
            "original_mime": mime_type,       # real MIME for vision block type selection
        })

        log.info(
            "_fetch_via_composio: %s — raw=%d bytes  text=%d chars  storage_path=%s",
            filename, len(raw_bytes), len(extracted_text), storage_path or "(not uploaded)",
        )

    return FetchEmailResult(
        message_id=gmail_message_id,   # Gmail hex ID used as dedup key
        subject=subject,
        sender=sender,
        body_text=body_text,
        attachments=attachments,
    )


# ── Activity ───────────────────────────────────────────────────────────────────

@activity.defn
def fetch_email_and_attachments(inp: FetchEmailInput) -> FetchEmailResult:
    """Load an email and its attachments.

    Inputs:
        inp.message_id:          Gmail hex ID (live) or fixture run ID (fixture mode)
        inp.use_fixture:         True → return fixture_* fields verbatim (eval/self-heal)
                                 False → call Composio Gmail live
        inp.fixture_subject / fixture_sender / fixture_body / fixture_attachments:
                                 Used only when use_fixture=True

    Outputs:
        FetchEmailResult(message_id, subject, sender, body_text, attachments)
        attachments: list of Attachment-dicts {name, mime, data_b64[, storage_path, original_mime]}

    Live mode env requirements:
        COMPOSIO_API_KEY, COMPOSIO_CONNECTED_ACCOUNT_ID, SUPABASE_URL, SUPABASE_SERVICE_KEY
    """
    log.info(
        "fetch_email_and_attachments message_id=%s fixture=%s attachments=%d",
        inp.message_id, inp.use_fixture, len(inp.fixture_attachments),
    )
    try:
        if inp.use_fixture:
            return FetchEmailResult(
                message_id=inp.message_id,
                subject=inp.fixture_subject,
                sender=inp.fixture_sender,
                body_text=inp.fixture_body,
                attachments=inp.fixture_attachments,
            )
        return _fetch_via_composio(inp.message_id)
    except ApplicationError:
        raise
    except Exception as exc:
        log.exception("fetch_email_and_attachments failed message_id=%s", inp.message_id)
        raise ApplicationError(
            f"fetch_email_and_attachments failed for message_id={inp.message_id!r}: {exc}",
            non_retryable=False,
        ) from exc


# ── Smoke test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import base64 as _b64

    inp = FetchEmailInput(
        message_id="test-001",
        use_fixture=True,
        fixture_subject="Test notification",
        fixture_sender="sender@example.com",
        fixture_body="Body text here.",
        fixture_attachments=[
            {
                "name": "doc.txt",
                "mime": "text/plain",
                "data_b64": _b64.b64encode(b"Field A: value\nField B: value").decode(),
            }
        ],
    )
    result = fetch_email_and_attachments(inp)
    print("fetch_email_and_attachments (fixture) OK")
    print(f"  message_id={result.message_id} subject={result.subject!r}")
    print(f"  attachments={len(result.attachments)} body_len={len(result.body_text)}")
    assert result.message_id == "test-001"
    assert result.subject == "Test notification"
    assert len(result.attachments) == 1
    print("  smoke test PASSED")
