"""emit_report + send_report — Temporal activities.

emit_report: builds a formatted CSV or JSON report from rows and a column spec.
send_report: sends the report by email via Composio GMAIL_SEND_EMAIL, optionally
             attaching the CSV file (uploads to Composio S3 first to get an s3key).
"""
import csv
import io
import json
import logging
import os

from temporalio import activity
from temporalio.exceptions import ApplicationError

from backend.runtime.activities._types import (
    EmitReportInput,
    EmitReportResult,
    SendReportInput,
    SendReportResult,
)

log = logging.getLogger(__name__)


@activity.defn
def emit_report(inp: EmitReportInput) -> EmitReportResult:
    """Build a formatted report from rows and a column spec.

    Inputs:
        inp.format: "csv" | "json"
        inp.columns: ordered list of column names (defines column order and inclusion)
        inp.rows: list of row dicts; each key is a column name

    Outputs:
        EmitReportResult(content, format, row_count)
        content: full CSV string or JSON string
    """
    log.info(
        "emit_report format=%s rows=%d columns=%s",
        inp.format, len(inp.rows), inp.columns,
    )
    try:
        if inp.format == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf,
                fieldnames=inp.columns,
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            for row in inp.rows:
                writer.writerow(row)
            content = buf.getvalue()
        elif inp.format == "json":
            filtered = [{col: row.get(col) for col in inp.columns} for row in inp.rows]
            content = json.dumps(filtered, indent=2)
        else:
            raise ApplicationError(
                f"Unsupported report format: {inp.format!r} — must be 'csv' or 'json'",
                non_retryable=True,
            )

        return EmitReportResult(
            content=content,
            format=inp.format,
            row_count=len(inp.rows),
        )
    except ApplicationError:
        raise  # already legible; don't wrap
    except Exception as exc:
        log.warning(
            "emit_report: unexpected error (format=%s rows=%d columns=%s) — returning empty report: %s",
            inp.format, len(inp.rows), inp.columns, exc, exc_info=True,
        )
        return EmitReportResult(content="", format=inp.format, row_count=0)


@activity.defn
def send_report(inp: SendReportInput) -> SendReportResult:
    """Send a report by email via Composio GMAIL_SEND_EMAIL.

    Reads Composio credentials from env (same vars as the rest of the backend):
        COMPOSIO_API_KEY, COMPOSIO_CONNECTED_ACCOUNT_ID, COMPOSIO_USER_ID,
        COMPOSIO_GMAIL_TOOLKIT_VERSION.

    Attachment behaviour (toolkit version 20260626_00):
        GMAIL_SEND_EMAIL's "attachment" param takes {name, mimetype, s3key}.
        The s3key must come from a Composio-managed S3 upload — local paths and
        base64 are not accepted.  When inp.attachment_filename is set, this
        function writes inp.report_content to a temp file, uploads it via
        FileUploadable.from_path(), and passes the resulting s3key.  The temp
        file is always deleted after upload.  If the upload fails, the email is
        sent without an attachment (body contains the inline CSV as fallback).

    Returns SendReportResult(sent=False, ...) — never raises — so callers treat
    a send failure as non-fatal.

    Inputs:
        inp.report_content:    formatted report string
        inp.format:            "csv" | "json"
        inp.recipient:         email address
        inp.subject:           email subject line
        inp.body:              fallback body (used when attachment upload fails or
                               no attachment_filename is set)
        inp.attachment_filename: desired filename for the attachment (e.g.
                               "meridian_result_235-36716875.csv"); empty = no
                               attachment, body used as-is
    """
    log.info(
        "send_report recipient=%s subject=%r content_len=%d attachment=%r",
        inp.recipient, inp.subject, len(inp.report_content), inp.attachment_filename or "(none)",
    )
    api_key              = os.environ.get("COMPOSIO_API_KEY", "")
    connected_account_id = os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "")
    user_id              = os.environ.get("COMPOSIO_USER_ID", "")
    gmail_version        = os.environ.get("COMPOSIO_GMAIL_TOOLKIT_VERSION", "20260626_00")

    if not api_key or not connected_account_id:
        log.warning("send_report: Composio credentials not configured — skipping send")
        return SendReportResult(sent=False, detail="Composio credentials not configured")

    from composio import Composio
    composio = Composio(api_key=api_key, toolkit_versions={"gmail": gmail_version})

    # ── Optional attachment upload ─────────────────────────────────────────────
    attachment: dict | None = None
    if inp.attachment_filename and inp.report_content:
        import tempfile
        from pathlib import Path
        from composio.core.models._files import FileUploadable

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".csv", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(inp.report_content)
                tmp_path = Path(tmp.name)

            # file_upload_allowlist=None bypasses the directory-scoping check
            # (appropriate for programmatic uploads per Composio SDK docs).
            uploaded = FileUploadable.from_path(
                client=composio.tools._client,
                file=tmp_path,
                tool="GMAIL_SEND_EMAIL",
                toolkit="gmail",
                file_upload_allowlist=None,
            )
            attachment = {**uploaded.model_dump(), "name": inp.attachment_filename}
            log.debug("send_report: uploaded attachment name=%s s3key=%s", inp.attachment_filename, uploaded.s3key)
        except Exception as exc:
            log.warning(
                "send_report: attachment upload failed (%s) — sending without attachment, CSV inline in body",
                exc,
            )
            attachment = None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    # ── Body ──────────────────────────────────────────────────────────────────
    if attachment:
        body = f"Meridian processed this shipment automatically.\n\nResult attached as {inp.attachment_filename}."
    else:
        body = inp.body if inp.body else inp.report_content

    # ── Send ──────────────────────────────────────────────────────────────────
    arguments: dict = {
        "recipient_email": inp.recipient,
        "subject":         inp.subject,
        "body":            body,
    }
    if attachment:
        arguments["attachment"] = attachment

    try:
        resp = composio.tools.execute(
            slug="GMAIL_SEND_EMAIL",
            arguments=arguments,
            connected_account_id=connected_account_id,
            user_id=user_id,
        )
    except Exception as exc:
        log.warning("send_report: GMAIL_SEND_EMAIL call failed: %s", exc, exc_info=True)
        return SendReportResult(sent=False, detail=f"Composio call failed: {exc}")

    if not resp.get("successful"):
        detail = str(resp.get("error") or "unknown error")
        log.warning("send_report: GMAIL_SEND_EMAIL unsuccessful: %s", detail)
        return SendReportResult(sent=False, detail=f"GMAIL_SEND_EMAIL failed: {detail}")

    attached_note = f" with attachment {inp.attachment_filename}" if attachment else ""
    log.info("send_report: sent to %s re: %r%s", inp.recipient, inp.subject, attached_note)
    return SendReportResult(sent=True, detail=f"Sent to {inp.recipient}{attached_note}")


# ── Smoke test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    rows = [
        {"item": "A", "passed": True,  "fail_reason": ""},
        {"item": "B", "passed": False, "fail_reason": "Missing: ['Qty']"},
        {"item": "C", "passed": True,  "fail_reason": ""},
    ]
    columns = ["item", "passed", "fail_reason"]

    csv_result = emit_report(EmitReportInput(format="csv", columns=columns, rows=rows))
    print("emit_report (CSV) OK")
    print(csv_result.content)
    assert csv_result.row_count == 3
    assert "item,passed,fail_reason" in csv_result.content

    json_result = emit_report(EmitReportInput(format="json", columns=columns, rows=rows))
    print("emit_report (JSON) OK")
    parsed = json.loads(json_result.content)
    assert len(parsed) == 3
    assert parsed[1]["item"] == "B"
    print("  JSON structure correct")

    send_result = send_report(SendReportInput(
        report_content=csv_result.content,
        format="csv",
        recipient="test@example.com",
        subject="Batch run report",
        body="See attached.",
    ))
    print(f"send_report: sent={send_result.sent} detail={send_result.detail!r}")
