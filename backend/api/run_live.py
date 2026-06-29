"""POST /api/v1/boards/{board_id}/run-live — run the generated agent on the latest real Gmail.

Flow:
    1. Validate: generated agent exists for this board.
    2. Find the latest Gmail message matching GMAIL_QUERY (Composio GMAIL_FETCH_EMAILS,
       ids_only=True — lightweight discovery call in the API layer).
    3. Check dedup: skip if this Gmail message was already processed for this board.
    4. Execute the generated Temporal workflow with use_fixture=False, message_id=<gmail_hex_id>.
       The fetch_email_and_attachments activity fetches the full message + attachments live.
    5. Parse the CSV result → 8 counts.
    6. Persist to agent_runs table.
    7. Return the run record (counts + csv_content + message_id + status).

Required env vars (add to backend/.env):
    COMPOSIO_API_KEY               already present
    COMPOSIO_CONNECTED_ACCOUNT_ID  the Gmail connected-account ID from the Composio dashboard
                                   (Settings → Connected Accounts → copy the Account ID)

Optional env vars:
    GMAIL_QUERY  Gmail search query for finding the target email.
                 Default: "has:attachment"
                 Example: "subject:pre-alert has:attachment"

SQL (run once in Supabase dashboard):
    See supabase/schema.sql — agent_runs table.
"""

import logging
import os
import pathlib
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields as dc_fields, is_dataclass
from datetime import timedelta, datetime, timezone

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException

load_dotenv(dotenv_path=pathlib.Path(__file__).parent.parent / ".env")

log = logging.getLogger(__name__)

_REPO_ROOT  = pathlib.Path(__file__).parent.parent.parent
_AGENTS_DIR = _REPO_ROOT / "backend" / "agents" / "generated"

# Ensure repo root on sys.path so backend.* is importable
_REPO_ROOT_STR = str(_REPO_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

router = APIRouter(prefix="/api/v1/boards", tags=["run-live"])


# ── Gmail discovery (lightweight — API layer only) ────────────────────────────

def _find_latest_gmail_message_id(
    api_key: str,
    connected_account_id: str,
    query: str,
    composio_user_id: str,
) -> str:
    """Return the Gmail hex ID of the latest message matching query.

    Calls GMAIL_FETCH_EMAILS with ids_only=True (fastest option — no body/attachment fetch).
    Raises HTTPException if no messages found or Composio call fails.
    """
    from composio import Composio

    gmail_version = os.environ.get("COMPOSIO_GMAIL_TOOLKIT_VERSION", "20260626_00")
    composio = Composio(api_key=api_key, toolkit_versions={"gmail": gmail_version})
    try:
        resp = composio.tools.execute(
            slug="GMAIL_FETCH_EMAILS",
            arguments={
                "query":      query,
                "max_results": 1,
                "ids_only":   True,
                "user_id":    "me",
            },
            connected_account_id=connected_account_id,
            user_id=composio_user_id,
        )
    except Exception as exc:
        raise HTTPException(502, f"Composio GMAIL_FETCH_EMAILS failed: {exc}") from exc

    if not resp.get("successful"):
        raise HTTPException(
            502, f"GMAIL_FETCH_EMAILS failed: {resp.get('error', 'unknown error')}"
        )

    messages = (resp.get("data") or {}).get("messages") or []
    if not messages:
        raise HTTPException(
            404,
            f"No Gmail messages found matching query {query!r}. "
            "Send a test email or adjust GMAIL_QUERY.",
        )

    gmail_id = (messages[0] or {}).get("messageId", "")
    if not gmail_id:
        raise HTTPException(502, "GMAIL_FETCH_EMAILS returned a message with no messageId")

    log.info("_find_latest_gmail_message_id: found gmail_id=%s (query=%r)", gmail_id, query)
    return gmail_id


# ── Temporal workflow execution (live mode) ───────────────────────────────────

async def _execute_live_workflow(board_id: str, gmail_message_id: str) -> dict:
    """Run the generated agent with use_fixture=False and return parsed output.

    Reuses the same Temporal infrastructure as runner.py but builds a live-mode input:
        message_id=<gmail_hex_id>, use_fixture=False, fixture_* = empty

    Returns:
        {"subject": str, "csv_content": str, "result_json": dict}
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
    from backend.agents.run_generated import (
        _load_generated_module, _find_workflow_class, _find_input_class,
    )
    from backend.evals.runner import _parse_csv

    ALL_ACTIVITIES = [
        classify_documents, extract_email_facts, fetch_email_and_attachments, load_document,
        validate_required_fields, match_by_key, tally, emit_report, send_report,
        is_email_processed, mark_email_processed,
    ]

    # Load generated workflow
    module       = _load_generated_module(board_id)
    workflow_cls = _find_workflow_class(module)
    input_cls    = _find_input_class(workflow_cls)

    # Build live-mode input
    kwargs: dict = {}
    if is_dataclass(input_cls):
        field_names = {f.name for f in dc_fields(input_cls)}
        if "message_id"          in field_names: kwargs["message_id"]          = gmail_message_id
        if "use_fixture"         in field_names: kwargs["use_fixture"]          = False
        if "board_id"            in field_names: kwargs["board_id"]             = board_id
        if "fixture_subject"     in field_names: kwargs["fixture_subject"]      = ""
        if "fixture_sender"      in field_names: kwargs["fixture_sender"]       = ""
        if "fixture_body"        in field_names: kwargs["fixture_body"]         = ""
        if "fixture_attachments" in field_names: kwargs["fixture_attachments"]  = []

    inp         = input_cls(**kwargs)
    workflow_id = f"live-{board_id[:8]}-{uuid.uuid4()}"
    address     = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")

    log.info(
        "run_live: connecting to Temporal at %s workflow_id=%s",
        address, workflow_id,
    )

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

    raw_csv = getattr(result, "report_content", "") or ""
    actual  = _parse_csv(raw_csv)
    subject = getattr(result, "shipment_number", "") or actual.get("shipment_number", "")

    log.info("run_live: workflow done  subject=%r  counts=%s", subject, actual)
    return {
        "subject":     subject,
        "csv_content": raw_csv,
        "result_json": actual,
    }


# ── Forced re-run: clear dedup record ────────────────────────────────────────

def _clear_dedup(board_id: str, message_id: str) -> None:
    """Delete the processed_emails row so the workflow treats this as a fresh email.

    Called only when force=True. Missing row and Supabase errors are both
    silently ignored — the workflow fails-open on dedup anyway.
    """
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        log.debug("_clear_dedup: Supabase not configured — skip")
        return
    try:
        from supabase import create_client
        sb = create_client(url, key)
        sb.table("processed_emails") \
          .delete() \
          .eq("message_id", message_id) \
          .eq("board_id", board_id) \
          .execute()
        log.info(
            "_clear_dedup: cleared dedup for message_id=%s board_id=%s",
            message_id, board_id,
        )
    except Exception as exc:
        log.warning(
            "_clear_dedup: failed for message_id=%s — continuing: %s",
            message_id, exc,
        )


# ── Persistence ───────────────────────────────────────────────────────────────

def _persist_agent_run(
    board_id: str,
    message_id: str,
    run_data: dict,
    status: str = "completed",
) -> dict:
    """Insert a row into agent_runs and return it (with id + created_at set)."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    row: dict = {
        "board_id":    board_id,
        "message_id":  message_id,
        "subject":     run_data.get("subject", ""),
        "status":      status,
        "csv_content": run_data.get("csv_content", ""),
        "result_json": run_data.get("result_json", {}),
        "created_at":  datetime.now(timezone.utc).isoformat(),
    }
    if url and key:
        try:
            from supabase import create_client
            sb  = create_client(url, key)
            res = sb.table("agent_runs").insert(row).execute()
            if res.data:
                row["id"] = res.data[0].get("id", "")
                row["created_at"] = res.data[0].get("created_at", row["created_at"])
        except Exception as exc:
            log.warning("_persist_agent_run: Supabase insert failed: %s", exc, exc_info=True)
    else:
        row["id"] = str(uuid.uuid4())
    return row


# ── FastAPI endpoint ──────────────────────────────────────────────────────────

@router.post("/{board_id}/run-live")
async def run_live(board_id: str, force: bool = False) -> dict:
    """Run the generated agent on the latest Gmail message matching GMAIL_QUERY.

    Returns the run record:
        {id, board_id, message_id, subject, status, csv_content, result_json, created_at}

    result_json contains the 8 report columns:
        {shipment_number, invoices_processed, invoices_succeeded, invoices_failed,
         goods_failed, batches_processed, batches_succeeded, batches_failed}

    Required env (backend/.env):
        COMPOSIO_API_KEY, COMPOSIO_CONNECTED_ACCOUNT_ID
    Optional:
        GMAIL_QUERY (default: "has:attachment")
    """
    # ── Validate: generated agent exists ─────────────────────────────────────
    safe_id    = board_id.replace("-", "_")
    agent_file = _AGENTS_DIR / f"agent_{safe_id}.py"
    if not agent_file.exists():
        raise HTTPException(
            404,
            f"No generated agent for board {board_id}. "
            "Run POST /api/v1/boards/{board_id}/codegen first.",
        )

    # ── Composio config ───────────────────────────────────────────────────────
    api_key = os.environ.get("COMPOSIO_API_KEY", "")
    connected_account_id = os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "")
    composio_user_id = os.environ.get("COMPOSIO_USER_ID", "")
    if not api_key:
        raise HTTPException(503, "COMPOSIO_API_KEY not configured in backend/.env")
    if not connected_account_id:
        raise HTTPException(
            503,
            "COMPOSIO_CONNECTED_ACCOUNT_ID not configured. "
            "Connect a Gmail account at app.composio.dev, copy the connected-account ID "
            "from Settings → Connected Accounts, and add it to backend/.env.",
        )
    if not composio_user_id:
        raise HTTPException(
            503,
            "COMPOSIO_USER_ID not configured. "
            "Set it to the entity/user ID the Gmail account was connected under "
            "(e.g. COMPOSIO_USER_ID=meridian-pharma) in backend/.env.",
        )

    gmail_query = os.environ.get("GMAIL_QUERY", "has:attachment")

    # ── Step 1: discover latest Gmail message ID (fast, lightweight) ──────────
    try:
        gmail_message_id = _find_latest_gmail_message_id(
            api_key, connected_account_id, gmail_query, composio_user_id
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("run_live: gmail discovery failed")
        raise HTTPException(502, f"Gmail discovery failed: {exc}") from exc

    # ── Optional: force re-process by clearing the dedup record ──────────────
    if force:
        _clear_dedup(board_id, gmail_message_id)

    # ── Step 2: execute the workflow (fetches + processes the email live) ─────
    run_data: dict
    try:
        run_data = await _execute_live_workflow(board_id, gmail_message_id)
        status   = "completed"
    except Exception as exc:
        log.exception("run_live: workflow execution failed board_id=%s gmail_id=%s", board_id, gmail_message_id)
        run_data = {"subject": "", "csv_content": "", "result_json": {}}
        status   = "failed"
        # Persist the failure record before re-raising
        row = _persist_agent_run(board_id, gmail_message_id, run_data, status=status)
        row["error"] = str(exc)
        raise HTTPException(500, f"Workflow execution failed: {exc}") from exc

    # ── Step 3: persist + return ──────────────────────────────────────────────
    row = _persist_agent_run(board_id, gmail_message_id, run_data, status=status)
    log.info(
        "run_live: done board_id=%s message_id=%s status=%s",
        board_id, gmail_message_id, status,
    )
    return row


# ── GET latest runs (for UI history list) ─────────────────────────────────────

@router.get("/{board_id}/runs")
async def list_runs(board_id: str, limit: int = 5) -> list[dict]:
    """Return the latest agent_runs for this board (newest first)."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return []
    try:
        from supabase import create_client
        sb  = create_client(url, key)
        res = (
            sb.table("agent_runs")
            .select("id, board_id, message_id, subject, status, result_json, created_at")
            .eq("board_id", board_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.warning("list_runs failed: %s", exc)
        return []
