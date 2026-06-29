"""Worked-example capture endpoint.

POST /api/v1/boards/{board_id}/worked-example  (multipart/form-data)
  Fields:
    email             UploadFile   — the example email (txt/pdf/any)
    attachments       UploadFile[] — N attachment files (invoices, COAs, etc.)
    expected_output   UploadFile   — expected results (CSV primary; xlsx best-effort)
    fixture_subject   str optional — shipment key to pass as email subject (e.g. MAWB); "" = auto

Behavior:
- Uploads email+attachments to Supabase storage at worked-examples/{board_id}/
- Replaces sample_files rows for this board (idempotent)
- Parses expected_output into an answer key (8 report columns)
- Writes eval case JSON to backend/evals/cases/board_{board_id[:8]}.json
- Returns {captured, case_path, answer_key, needs_confirmation}

needs_confirmation=True when the expected_output couldn't be fully parsed into the 8 columns —
the agent can still be built; you'll just see placeholder values in the eval.
"""

import csv
import io
import json
import logging
import os
import pathlib
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from supabase import create_client

router = APIRouter(prefix="/api/v1/boards", tags=["worked-example"])
log = logging.getLogger(__name__)

BUCKET          = "sample-files"
STORAGE_PREFIX  = "worked-examples"
TEXT_CAP        = 20_000
CASES_DIR       = pathlib.Path(__file__).parent.parent / "evals" / "cases"

# The 8 report columns the agent produces
ANSWER_KEY_FIELDS = [
    "shipment_number",
    "invoices_processed",
    "invoices_succeeded",
    "invoices_failed",
    "goods_failed",
    "batches_processed",
    "batches_succeeded",
    "batches_failed",
]

# Integer fields — coerce if parseable
_INT_FIELDS = {
    "invoices_processed", "invoices_succeeded", "invoices_failed",
    "goods_failed", "batches_processed", "batches_succeeded", "batches_failed",
}


def _sb():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise HTTPException(503, "Supabase not configured")
    return create_client(url, key)


def _extract_text(content: bytes, filename: str, mime: str) -> str:
    name_lower = (filename or "").lower()
    if mime == "application/pdf" or name_lower.endswith(".pdf"):
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)[:TEXT_CAP]
        except Exception as exc:
            log.warning("pdfplumber failed for %s: %s", filename, exc)
            return ""
    if mime in ("text/plain", "text/csv") or name_lower.endswith((".txt", ".csv")):
        try:
            return content.decode("utf-8", errors="replace")[:TEXT_CAP]
        except Exception:
            return ""
    return ""


def _coerce_value(field: str, raw: str):
    """Coerce raw string to int for integer fields, leave as str otherwise."""
    stripped = (raw or "").strip()
    if not stripped:
        return None
    if field in _INT_FIELDS:
        try:
            return int(stripped)
        except ValueError:
            try:
                return int(float(stripped))
            except ValueError:
                return stripped
    return stripped or None


def _parse_csv_expected(content: bytes) -> tuple[dict, bool]:
    """Parse CSV bytes → answer_key dict. Returns (answer_key, fully_parsed)."""
    text = content.decode("utf-8", errors="replace").strip()
    try:
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return {f: None for f in ANSWER_KEY_FIELDS}, False
        row = rows[0]
        # Normalize header keys (strip whitespace, lowercase)
        row_norm = {k.strip().lower(): v for k, v in row.items()}
        answer_key: dict = {}
        any_found = False
        for field in ANSWER_KEY_FIELDS:
            raw = row_norm.get(field)
            if raw is not None:
                answer_key[field] = _coerce_value(field, raw)
                any_found = True
            else:
                answer_key[field] = None
        return answer_key, any_found
    except Exception as exc:
        log.warning("CSV parse failed: %s", exc)
        return {f: None for f in ANSWER_KEY_FIELDS}, False


def _parse_xlsx_expected(content: bytes) -> tuple[dict, bool]:
    """Parse xlsx bytes → answer_key dict. Returns (answer_key, fully_parsed)."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            return {f: None for f in ANSWER_KEY_FIELDS}, False
        headers = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
        values  = rows[1]
        row_map = {headers[i]: values[i] for i in range(min(len(headers), len(values)))}
        answer_key: dict = {}
        any_found = False
        for field in ANSWER_KEY_FIELDS:
            v = row_map.get(field)
            if v is not None:
                answer_key[field] = _coerce_value(field, str(v))
                any_found = True
            else:
                answer_key[field] = None
        return answer_key, any_found
    except ImportError:
        log.warning("openpyxl not installed — xlsx best-effort not available")
        return {f: None for f in ANSWER_KEY_FIELDS}, False
    except Exception as exc:
        log.warning("xlsx parse failed: %s", exc)
        return {f: None for f in ANSWER_KEY_FIELDS}, False


def _parse_text_expected(text: str) -> tuple[dict, bool]:
    """Try to find field=value patterns in free-form text (pdf/docx fallback)."""
    import re
    answer_key: dict = {f: None for f in ANSWER_KEY_FIELDS}
    any_found = False
    for field in ANSWER_KEY_FIELDS:
        # e.g. "invoices_processed: 3" or "invoices_processed = 3" or "invoices processed  3"
        pattern = re.compile(
            r'\b' + re.escape(field.replace("_", r"[_ ]?")) + r'\b\s*[:=]\s*(\S+)',
            re.IGNORECASE,
        )
        m = pattern.search(text)
        if m:
            answer_key[field] = _coerce_value(field, m.group(1))
            any_found = True
    return answer_key, any_found


def _parse_expected_output(content: bytes, filename: str, mime: str) -> tuple[dict, bool]:
    """Parse expected output file into answer_key. Returns (answer_key, needs_confirmation)."""
    name_lower = (filename or "").lower()

    if name_lower.endswith(".csv") or mime == "text/csv":
        answer_key, ok = _parse_csv_expected(content)
        return answer_key, not ok

    if name_lower.endswith((".xlsx", ".xls")):
        answer_key, ok = _parse_xlsx_expected(content)
        return answer_key, not ok

    # PDF / docx / txt — extract text and try pattern search
    text = _extract_text(content, filename, mime)
    if not text and mime == "text/plain":
        try:
            text = content.decode("utf-8", errors="replace")[:TEXT_CAP]
        except Exception:
            pass

    if text:
        answer_key, ok = _parse_text_expected(text)
        return answer_key, not ok

    # Couldn't extract anything useful
    log.warning("Could not parse expected_output %s (mime=%s) — placeholders inserted", filename, mime)
    return {f: None for f in ANSWER_KEY_FIELDS}, True


def _upload_to_storage(sb, content: bytes, board_id: str, filename: str, mime: str) -> str:
    """Upload bytes to sample-files bucket. Returns storage_path."""
    safe_name = filename.replace(" ", "_")
    storage_path = f"{STORAGE_PREFIX}/{board_id}/{safe_name}"
    try:
        # Try upsert-style: delete first, then upload (Supabase storage has no native upsert)
        try:
            sb.storage.from_(BUCKET).remove([storage_path])
        except Exception:
            pass
        sb.storage.from_(BUCKET).upload(
            path=storage_path,
            file=content,
            file_options={"content-type": mime or "application/octet-stream"},
        )
    except Exception as exc:
        log.warning("Storage upload failed for %s: %s — continuing without storage_path", filename, exc)
        return ""
    return storage_path


@router.post("/{board_id}/worked-example")
async def capture_worked_example(
    board_id: str,
    email: UploadFile = File(...),
    attachments: List[UploadFile] = File(default=[]),
    expected_output: UploadFile = File(...),
    fixture_subject: Optional[str] = Form(default=""),
) -> dict:
    """Upload a worked example (email + attachments + expected CSV) for this board.

    Replaces the board's sample_files and writes an eval case the build-agent
    endpoint can score against. Idempotent — re-upload replaces.
    """
    try:
        sb = _sb()

        # ── 1. Clear existing sample_files for this board ─────────────────────
        sb.table("sample_files").delete().eq("board_id", board_id).execute()
        log.info("worked-example: cleared old sample_files for board %s", board_id)

        sample_file_rows: list[dict] = []
        now_ts = datetime.now(timezone.utc).isoformat()

        # ── 2. Upload + register the email file ───────────────────────────────
        email_bytes   = await email.read()
        email_name    = email.filename or "sample_email.txt"
        email_mime    = email.content_type or "text/plain"
        email_storage = _upload_to_storage(sb, email_bytes, board_id, f"sample_email_{pathlib.Path(email_name).suffix or '.txt'}", email_mime)
        email_text    = _extract_text(email_bytes, email_name, email_mime)

        email_row = {
            "board_id":       board_id,
            "node_id":        None,
            "filename":       "sample_email" + (pathlib.Path(email_name).suffix or ".txt"),
            "mime":           email_mime,
            "storage_path":   email_storage,
            "extracted_text": email_text or "",
        }
        sample_file_rows.append(email_row)
        log.info("worked-example: email → %s (%d chars)", email_row["filename"], len(email_text or ""))

        # ── 3. Upload + register each attachment ──────────────────────────────
        for att in attachments:
            att_bytes   = await att.read()
            att_name    = att.filename or f"attachment_{uuid.uuid4().hex[:8]}"
            att_mime    = att.content_type or "application/octet-stream"
            att_storage = _upload_to_storage(sb, att_bytes, board_id, att_name, att_mime)
            att_text    = _extract_text(att_bytes, att_name, att_mime)

            sample_file_rows.append({
                "board_id":       board_id,
                "node_id":        None,
                "filename":       att_name,
                "mime":           att_mime,
                "storage_path":   att_storage,
                "extracted_text": att_text or "",
            })
            log.info(
                "worked-example: attachment %s → storage_path=%s text=%d chars",
                att_name, att_storage or "(none)", len(att_text or ""),
            )

        # ── 4. Insert all sample_files rows ───────────────────────────────────
        if sample_file_rows:
            sb.table("sample_files").insert(sample_file_rows).execute()
        log.info("worked-example: inserted %d sample_files rows", len(sample_file_rows))

        # ── 5. Parse expected output → answer key ─────────────────────────────
        eo_bytes  = await expected_output.read()
        eo_name   = expected_output.filename or "expected.csv"
        eo_mime   = expected_output.content_type or "text/csv"
        answer_key, needs_confirmation = _parse_expected_output(eo_bytes, eo_name, eo_mime)
        log.info(
            "worked-example: parsed expected_output %s → needs_confirmation=%s  key=%s",
            eo_name, needs_confirmation, answer_key,
        )

        # ── 6. Write eval case JSON ────────────────────────────────────────────
        CASES_DIR.mkdir(parents=True, exist_ok=True)
        safe_id   = board_id.replace("-", "")[:8]
        case_path = CASES_DIR / f"board_{safe_id}_worked.json"

        case = {
            "_instructions": [
                f"Auto-generated eval case from worked-example upload for board {board_id}.",
                "Expected values were parsed from the uploaded expected_output file.",
                f"needs_confirmation={needs_confirmation} — set to false once you verify the values.",
                "Re-run: python -m backend.evals.evaluate " + board_id,
            ],
            "board_id":            board_id,
            "name":                f"Worked example — board {board_id[:8]}",
            "fixture_subject":     (fixture_subject or "").strip(),
            "email_filename_hint": "sample_email",
            "needs_confirmation":  needs_confirmation,
            "expected":            answer_key,
            "tolerances":          {},
        }
        case_path.write_text(json.dumps(case, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("worked-example: wrote eval case → %s", case_path)

        return {
            "captured":           True,
            "case_path":          str(case_path),
            "answer_key":         answer_key,
            "needs_confirmation": needs_confirmation,
            "files_uploaded":     len(sample_file_rows),
            "board_id":           board_id,
        }

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("capture_worked_example failed board_id=%s", board_id)
        raise HTTPException(500, f"worked-example upload failed: {exc}")
