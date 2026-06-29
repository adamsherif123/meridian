"""Eval runner: execute a generated agent with real fixtures and capture its CSV output.

General — works for any board whose agent has been generated via POST /api/v1/boards/{id}/codegen.
Pharma-specific values live only in the case JSON file, not here.

Usage (called by evaluate.py; also importable):
    result = asyncio.run(run_eval("a8d28f1d-...", "backend/evals/cases/board7_pharma.json"))
    # result: {actual: {field: value}, raw_csv: str, message_id: str, workflow_class: str}
"""
import asyncio
import csv
import io
import json
import logging
import os
import pathlib
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields as dc_fields, is_dataclass
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv(dotenv_path=pathlib.Path(__file__).parent.parent / ".env")

log = logging.getLogger(__name__)

# Ensure repo root is on sys.path so backend.* is importable
_REPO_ROOT = str(pathlib.Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── Module loading (reuse run_generated helpers) ──────────────────────────────

def _load_module_helpers():
    """Lazy import of run_generated helpers to avoid circular imports."""
    from backend.agents.run_generated import (
        _load_generated_module,
        _find_workflow_class,
        _find_input_class,
    )
    return _load_generated_module, _find_workflow_class, _find_input_class


# ── Supabase fixture loading ───────────────────────────────────────────────────

async def _load_fixtures(board_id: str, email_filename_hint: str) -> tuple[list[dict], str]:
    """Load sample_files → (fixture_attachments, fixture_body_text).

    fixture_attachments: all sample_files as text/plain dicts for the workflow input.
    fixture_body_text: extracted_text of the file matching email_filename_hint
                       (the email body, not an invoice/COA attachment).
    """
    import base64
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            log.warning("Supabase not configured — using empty fixtures")
            return [], ""

        sb = create_client(url, key)
        res = (
            sb.table("sample_files")
            .select("filename, mime, storage_path, extracted_text")
            .eq("board_id", board_id)
            .execute()
        )
        rows = (res.data or []) if res is not None else []

        if not rows:
            log.warning("No sample_files found for board_id=%s", board_id)
            return [], ""

        fixture_attachments: list[dict] = []
        email_body = ""

        for row in rows:
            filename = row.get("filename", "attachment") or "attachment"
            extracted_text = row.get("extracted_text", "") or ""
            data_b64 = base64.b64encode(extracted_text.encode()).decode() if extracted_text else ""

            storage_path  = row.get("storage_path", "") or ""
            original_mime = row.get("mime", "") or ""
            log.info(
                "Fixture file: %-40s  %d chars  storage_path=%s",
                filename, len(extracted_text), storage_path or "(none)",
            )
            fixture_attachments.append({
                "name":          filename,
                "mime":          "text/plain",   # overridden to stay within Temporal blob limit
                "data_b64":      data_b64,
                "storage_path":  storage_path,   # enables vision fallback in load_document
                "original_mime": original_mime,  # real MIME for vision block type selection
            })

            # Identify the email body file by the hint (case-insensitive substring)
            if email_filename_hint and email_filename_hint.lower() in filename.lower():
                email_body = extracted_text
                log.info("Email body: %s (%d chars)", filename, len(extracted_text))

        return fixture_attachments, email_body

    except Exception as exc:
        log.warning("_load_fixtures failed: %s — using empty fixtures", exc, exc_info=True)
        return [], ""


# ── CSV parsing ────────────────────────────────────────────────────────────────

def _parse_csv(content: str) -> dict:
    """Parse the agent's CSV report into a flat dict.

    Coerces numeric columns to int (or float), leaves string columns as-is.
    Returns {} if content is empty or has no data rows.
    """
    if not (content or "").strip():
        return {}
    try:
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        if not rows:
            return {}
        row = rows[0]
        parsed: dict = {}
        for k, v in row.items():
            stripped = (v or "").strip()
            try:
                parsed[k] = int(stripped)
            except (ValueError, TypeError):
                try:
                    parsed[k] = float(stripped)
                except (ValueError, TypeError):
                    parsed[k] = stripped
        return parsed
    except Exception as exc:
        log.warning("CSV parse failed: %s", exc)
        return {}


# ── Workflow input builder ─────────────────────────────────────────────────────

def _build_input(input_cls, *, message_id: str, board_id: str,
                 fixture_subject: str, fixture_body: str,
                 fixture_attachments: list[dict]):
    """Construct the workflow input dataclass from known fixture fields.

    Only sets fields that the input class actually declares — general,
    works for any generated agent's input class.
    """
    if not is_dataclass(input_cls):
        raise RuntimeError(f"Expected a dataclass input class, got {input_cls}")

    field_names = {f.name for f in dc_fields(input_cls)}
    kwargs: dict = {}
    if "message_id"           in field_names: kwargs["message_id"]           = message_id
    if "use_fixture"          in field_names: kwargs["use_fixture"]           = True
    if "board_id"             in field_names: kwargs["board_id"]              = board_id
    if "fixture_subject"      in field_names: kwargs["fixture_subject"]       = fixture_subject
    if "fixture_body"         in field_names: kwargs["fixture_body"]          = fixture_body
    if "fixture_sender"       in field_names: kwargs["fixture_sender"]        = ""
    if "fixture_attachments"  in field_names: kwargs["fixture_attachments"]   = fixture_attachments

    return input_cls(**kwargs)


# ── Temporal execution ─────────────────────────────────────────────────────────

async def _execute_workflow(workflow_cls, inp, board_id: str) -> object:
    """Run the generated workflow with an embedded Temporal worker.

    Mirrors the pattern in run_generated.py: the worker lives only for
    the duration of the workflow execution.
    """
    from temporalio.client import Client
    from temporalio.worker import Worker
    from backend.runtime.worker import TASK_QUEUE
    from backend.runtime.activities.classify_documents import classify_documents
    from backend.runtime.activities.extract_email_facts import extract_email_facts
    from backend.runtime.activities.fetch_email import fetch_email_and_attachments
    from backend.runtime.activities.load_document import load_document
    from backend.runtime.activities.validate_fields import validate_required_fields
    from backend.runtime.activities.match_documents import match_by_key
    from backend.runtime.activities.tally import tally
    from backend.runtime.activities.report import emit_report, send_report
    from backend.runtime.activities.email_dedup import is_email_processed, mark_email_processed

    ALL_ACTIVITIES = [
        classify_documents, extract_email_facts, fetch_email_and_attachments, load_document,
        validate_required_fields, match_by_key, tally, emit_report, send_report,
        is_email_processed, mark_email_processed,
    ]

    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    workflow_id = f"eval-{board_id[:8]}-{uuid.uuid4()}"

    log.info("eval: connecting to Temporal at %s workflow_id=%s", address, workflow_id)
    client = await Client.connect(address)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[workflow_cls],
        activities=ALL_ACTIVITIES,
        activity_executor=ThreadPoolExecutor(max_workers=10),
    )

    async with worker:
        result = await client.execute_workflow(
            workflow_cls.run,
            inp,
            id=workflow_id,
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(minutes=20),
        )

    log.info("eval: workflow completed workflow_id=%s", workflow_id)
    return result


# ── Public API ─────────────────────────────────────────────────────────────────

async def run_eval(board_id: str, case_path: str | pathlib.Path) -> dict:
    """Execute the generated agent with real fixtures and return structured output.

    Args:
        board_id:  UUID of the board whose generated agent to run.
        case_path: Path to the eval case JSON file (for fixture overrides).

    Returns:
        {
            "actual":         {field: value, ...}  — parsed from the CSV report,
            "raw_csv":        str                  — the agent's full CSV output,
            "message_id":     str                  — the run's unique message-id,
            "workflow_class": str                  — name of the workflow class run,
        }
    """
    case = json.loads(pathlib.Path(case_path).read_text(encoding="utf-8"))

    fixture_subject      = case.get("fixture_subject", "")
    email_filename_hint  = case.get("email_filename_hint", "email")

    log.info("run_eval board_id=%s case=%s", board_id, case.get("name", case_path))

    # ── 1. Load fixture attachments + email body from Supabase ────────────
    fixture_attachments, fixture_body = await _load_fixtures(board_id, email_filename_hint)
    log.info(
        "run_eval: %d attachments, email_body=%d chars, subject=%r",
        len(fixture_attachments), len(fixture_body), fixture_subject,
    )

    # ── 2. Load the generated workflow module ─────────────────────────────
    _load_generated_module, _find_workflow_class, _find_input_class = _load_module_helpers()
    module         = _load_generated_module(board_id)
    workflow_cls   = _find_workflow_class(module)
    input_cls      = _find_input_class(workflow_cls)

    # ── 3. Build workflow input with real fixtures ─────────────────────────
    message_id = f"eval-{uuid.uuid4()}"
    inp = _build_input(
        input_cls,
        message_id=message_id,
        board_id=board_id,
        fixture_subject=fixture_subject,
        fixture_body=fixture_body,
        fixture_attachments=fixture_attachments,
    )

    log.info(
        "run_eval: input class=%s message_id=%s fixture_subject=%r attachments=%d",
        input_cls.__name__, message_id, fixture_subject, len(fixture_attachments),
    )

    # ── 4. Execute the workflow ────────────────────────────────────────────
    result = await _execute_workflow(workflow_cls, inp, board_id)

    # ── 5. Parse the CSV output ────────────────────────────────────────────
    raw_csv = getattr(result, "report_content", "") or ""
    actual  = _parse_csv(raw_csv)

    # Also pull shipment_number directly from the result if the CSV is empty
    if "shipment_number" not in actual:
        sn = getattr(result, "shipment_number", "")
        if sn:
            actual["shipment_number"] = sn

    log.info("run_eval: actual=%s", actual)

    return {
        "actual":           actual,
        "raw_csv":          raw_csv,
        "message_id":       message_id,
        "workflow_class":   workflow_cls.__name__,
        "attachment_count": len(fixture_attachments),
    }
