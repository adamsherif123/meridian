"""File upload and text extraction endpoints."""

import io
import logging
import os
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from supabase import create_client

router = APIRouter(prefix="/api/v1/files", tags=["files"])
log = logging.getLogger(__name__)

BUCKET = "sample-files"
TEXT_CAP = 20_000


def _sb():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    return create_client(url, key)


def _extract_text(content: bytes, filename: str, mime: str) -> str | None:
    name_lower = (filename or "").lower()
    if mime == "application/pdf" or name_lower.endswith(".pdf"):
        try:
            import pdfplumber  # lazy — only imported when a PDF is uploaded
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages)[:TEXT_CAP]
        except Exception as exc:
            return f"[PDF extraction failed: {exc}]"
    if mime in ("text/plain", "text/csv") or name_lower.endswith((".txt", ".csv")):
        try:
            return content.decode("utf-8", errors="replace")[:TEXT_CAP]
        except Exception as exc:
            return f"[text extraction failed: {exc}]"
    return None  # binary / unsupported — stored without extracted text


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    board_id: str = Form(...),
    node_id: str = Form(...),
) -> dict:
    try:
        sb = _sb()
        content = await file.read()
        mime = file.content_type or "application/octet-stream"
        filename = file.filename or "upload"

        extracted_text = _extract_text(content, filename, mime)

        file_uuid = str(uuid.uuid4())
        storage_path = f"{board_id}/{node_id}/{file_uuid}-{filename}"

        try:
            sb.storage.from_(BUCKET).upload(
                path=storage_path,
                file=content,
                file_options={"content-type": mime},
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Storage upload failed: {exc}")

        res = (
            sb.table("sample_files")
            .insert({
                "board_id": board_id,
                "node_id": node_id,
                "filename": filename,
                "mime": mime,
                "storage_path": storage_path,
                "extracted_text": extracted_text,
            })
            .execute()
        )
        if not res.data:
            raise HTTPException(
                status_code=500,
                detail="sample_files insert returned no rows — check the sample_files table exists and RLS is disabled",
            )
        row = res.data[0]

        return {"file_id": row["id"], "filename": row["filename"], "mime": row["mime"]}
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("upload_file failed (board_id=%s, node_id=%s)", board_id, node_id)
        raise HTTPException(status_code=500, detail=f"upload_file failed: {exc}")


@router.get("/{file_id}/text")
async def get_file_text(file_id: str) -> dict:
    try:
        sb = _sb()
        result = (
            sb.table("sample_files")
            .select("filename, extracted_text")
            .eq("id", file_id)
            .maybe_single()
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="File not found")
        return {
            "filename": result.data["filename"],
            "extracted_text": result.data["extracted_text"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("get_file_text failed (file_id=%s)", file_id)
        raise HTTPException(status_code=500, detail=f"get_file_text failed: {exc}")
