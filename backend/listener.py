"""Background inbox listener — polls Gmail on a configurable interval and
automatically runs each board's generated agent on new messages that look
like shipment pre-alerts.

ADDITIVE: does not change the manual POST /run-live flow in any way.
Shares _execute_live_workflow and _persist_agent_run from run_live.py without
modifying that file. The gate check calls extract_email_facts directly
(no Temporal context required; the function has no activity.info() call).
Idempotency is guaranteed by the same processed_emails dedup table the manual
path uses — the workflow's own is_email_processed guard is the canonical check.

──────────────────────────────────────────────────────────────────────────────
START:
    backend/.venv/bin/python -m backend.listener

WATCH LOGS (foreground):
    backend/.venv/bin/python -m backend.listener 2>&1 | tee listener.log

BACKGROUND:
    nohup backend/.venv/bin/python -m backend.listener >> listener.log 2>&1 &
    tail -f listener.log

──────────────────────────────────────────────────────────────────────────────
ENV VARS
    LISTENER_INTERVAL_SECONDS   Poll interval in seconds          (default: 300)
    LISTENER_MAX_MESSAGES       Max recent messages per tick      (default: 20)
    LISTENER_BOARD_IDS          Comma-separated board IDs to run  (default: auto-discover)
    GMAIL_QUERY                 Gmail search query                (default: "has:attachment")
    COMPOSIO_API_KEY            Required
    COMPOSIO_CONNECTED_ACCOUNT_ID  Required
    COMPOSIO_USER_ID            Required
    COMPOSIO_GMAIL_TOOLKIT_VERSION  Optional
    ANTHROPIC_API_KEY           Required (gate check via extract_email_facts)
    SUPABASE_URL / SUPABASE_SERVICE_KEY  Required (dedup pre-check)
    TEMPORAL_ADDRESS            Temporal server                   (default: localhost:7233)

Email reporting (listener-only — the manual "Run on my inbox" path is unaffected):
    REPORT_EMAIL_ON_LISTENER    Set to "true" to email results after each auto-run
    REPORT_EMAIL_TO             Recipient address (required when emailing is on)
    If REPORT_EMAIL_TO is absent or REPORT_EMAIL_ON_LISTENER != "true", the email
    step is silently skipped; the agent_runs row is always written regardless.
"""
import asyncio
import logging
import os
import pathlib
import sys

from dotenv import load_dotenv

_HERE = pathlib.Path(__file__).parent
load_dotenv(dotenv_path=_HERE / ".env")

# Repo root on sys.path so backend.* imports resolve
_REPO_ROOT = str(_HERE.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("meridian.listener")

_GENERATED_DIR = _HERE / "agents" / "generated"


# ── Board discovery ────────────────────────────────────────────────────────────

def _discover_board_ids() -> list[str]:
    """Return board IDs for every generated agent on disk (skips .bak files).

    Parses the UUID from each agent_<uuid>.py filename; the filename uses
    underscores where the UUID uses hyphens.
    """
    ids: list[str] = []
    for path in sorted(_GENERATED_DIR.glob("agent_*.py")):
        if ".bak" in path.name:
            continue
        raw = path.stem[len("agent_"):]   # strip "agent_" prefix
        parts = raw.split("_", 4)          # UUIDs have 5 hyphen-separated groups
        if len(parts) == 5:
            ids.append("-".join(parts))
    return ids


# ── Composio helpers ───────────────────────────────────────────────────────────

def _composio_config() -> tuple[str, str, str, str]:
    """Return (api_key, connected_account_id, user_id, gmail_version)."""
    return (
        os.environ.get("COMPOSIO_API_KEY", ""),
        os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", ""),
        os.environ.get("COMPOSIO_USER_ID", ""),
        os.environ.get("COMPOSIO_GMAIL_TOOLKIT_VERSION", "20260626_00"),
    )


def _fetch_recent_message_ids(query: str, max_results: int) -> list[str]:
    """Return up to max_results recent Gmail message IDs matching query.

    Uses ids_only=True — same lightweight call as run_live, but with
    max_results > 1 so the listener inspects multiple messages per tick.
    Returns [] on any error so a Composio hiccup skips the tick gracefully.
    """
    from composio import Composio

    api_key, connected_account_id, user_id, gmail_version = _composio_config()
    if not api_key or not connected_account_id:
        log.warning("_fetch_recent_message_ids: Composio credentials not configured — skipping tick")
        return []

    composio = Composio(api_key=api_key, toolkit_versions={"gmail": gmail_version})
    try:
        resp = composio.tools.execute(
            slug="GMAIL_FETCH_EMAILS",
            arguments={
                "query":       query,
                "max_results": max_results,
                "ids_only":    True,
                "user_id":     "me",
            },
            connected_account_id=connected_account_id,
            user_id=user_id,
        )
    except Exception as exc:
        log.warning("_fetch_recent_message_ids: Composio call failed: %s", exc)
        return []

    if not resp.get("successful"):
        log.warning("_fetch_recent_message_ids: GMAIL_FETCH_EMAILS unsuccessful: %s", resp.get("error"))
        return []

    messages = (resp.get("data") or {}).get("messages") or []
    ids = [m.get("messageId", "") for m in messages if m.get("messageId")]
    log.info("_fetch_recent_message_ids: %d message IDs (query=%r)", len(ids), query)
    return ids


def _fetch_email_for_gate(message_id: str) -> tuple[str, str]:
    """Fetch (subject, body_text) for gate-checking only — not for the workflow.

    The workflow's fetch_email_and_attachments activity does its own full
    fetch including attachments; this is a cheap preview for the shipment gate.
    Returns ("", "") on any error; the gate conservatively skips on empty data.
    """
    from composio import Composio

    api_key, connected_account_id, user_id, gmail_version = _composio_config()
    composio = Composio(api_key=api_key, toolkit_versions={"gmail": gmail_version})
    try:
        resp = composio.tools.execute(
            slug="GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
            arguments={"message_id": message_id, "format": "full", "user_id": "me"},
            connected_account_id=connected_account_id,
            user_id=user_id,
        )
    except Exception as exc:
        log.warning("_fetch_email_for_gate: failed for %s: %s", message_id, exc)
        return ("", "")

    if not resp.get("successful"):
        log.warning("_fetch_email_for_gate: unsuccessful for %s: %s", message_id, resp.get("error"))
        return ("", "")

    data    = resp.get("data") or {}
    subject = (data.get("subject") or "").strip()
    body    = (data.get("messageText") or "").strip()
    return (subject, body)


# ── Shipment gate ──────────────────────────────────────────────────────────────

def _is_shipment_email(subject: str, body: str) -> bool:
    """Return True when extract_email_facts finds a shipment ID or invoice numbers.

    Uses the same LLM path the workflow uses. extract_email_facts has no
    activity.info() call so it is safe to invoke outside a Temporal context.
    Conservative: returns False on any error.
    """
    from backend.runtime.activities.extract_email_facts import extract_email_facts
    from backend.runtime.activities._types import ExtractEmailFactsInput

    if not subject and not body:
        return False
    try:
        result = extract_email_facts(
            ExtractEmailFactsInput(subject=subject, body=body, shipment_id_hint="", invoice_hint="")
        )
        verdict = bool(result.shipment_id or result.invoice_numbers)
        log.debug(
            "_is_shipment_email: shipment_id=%r invoices=%s → %s",
            result.shipment_id, result.invoice_numbers, verdict,
        )
        return verdict
    except Exception as exc:
        log.warning("_is_shipment_email: gate check failed (%s) — skipping", exc)
        return False


# ── Dedup pre-check ────────────────────────────────────────────────────────────

def _already_processed(message_id: str, board_id: str) -> bool:
    """Quick pre-check against processed_emails before starting a workflow.

    Calls is_email_processed directly (no activity.info() in that function).
    The workflow's own dedup guard is the canonical authority; this is a cheap
    optimisation to avoid spinning up a Temporal worker unnecessarily.
    Conservative: returns False on DB error (workflow guard will catch it).
    """
    from backend.runtime.activities.email_dedup import is_email_processed
    from backend.runtime.activities._types import EmailDedupInput

    try:
        result = is_email_processed(EmailDedupInput(message_id=message_id, board_id=board_id))
        return result.already_processed
    except Exception as exc:
        log.warning(
            "_already_processed: check failed for %s / %s: %s — treating as unprocessed",
            message_id, board_id, exc,
        )
        return False


# ── Email result (listener-only) ──────────────────────────────────────────────

def _safe_filename_tag(raw: str) -> str:
    """Produce a filesystem-safe tag from an arbitrary string (e.g. email subject).

    Keeps alphanumerics, hyphens, and dots; collapses everything else to '_';
    strips leading/trailing underscores; caps at 60 chars.
    """
    import re
    safe = re.sub(r"[^\w\-.]", "_", raw).strip("_")
    safe = re.sub(r"_+", "_", safe)
    return safe[:60]


def _maybe_email_result(run_data: dict) -> None:
    """Send the CSV result by email (with attachment) if both env vars are set.

    Silently skips if REPORT_EMAIL_ON_LISTENER != "true", REPORT_EMAIL_TO is
    absent, or the run produced no csv_content. Never raises.

    Attachment param used: GMAIL_SEND_EMAIL "attachment" → {name, mimetype, s3key}
    (toolkit version 20260626_00).  send_report uploads to Composio S3 and
    passes the s3key; on upload failure it falls back to inline CSV in body.
    """
    if os.environ.get("REPORT_EMAIL_ON_LISTENER", "").lower() != "true":
        return
    recipient = os.environ.get("REPORT_EMAIL_TO", "").strip()
    if not recipient:
        return
    csv_content = run_data.get("csv_content", "")
    if not csv_content:
        log.debug("_maybe_email_result: no csv_content in run_data — skipping email")
        return

    from backend.runtime.activities.report import send_report
    from backend.runtime.activities._types import SendReportInput

    email_subject_tag = (run_data.get("subject") or "auto-run").strip()
    safe_tag          = _safe_filename_tag(email_subject_tag)
    attachment_name   = f"meridian_result_{safe_tag}.csv"

    result = send_report(SendReportInput(
        report_content=csv_content,
        format="csv",
        recipient=recipient,
        subject=f"Meridian agent run — {email_subject_tag}",
        body=f"Meridian processed this shipment automatically.\n\nResult:\n{csv_content}",
        attachment_filename=attachment_name,
    ))
    if result.sent:
        log.info("_maybe_email_result: %s", result.detail)
    else:
        log.warning("_maybe_email_result: email not sent — %s", result.detail)


# ── Single poll tick ───────────────────────────────────────────────────────────

async def poll_once(query: str, board_ids: list[str], max_messages: int) -> None:
    """One listener tick.

    For each recent message:
      1. Skip boards that already processed it (dedup pre-check).
      2. Gate: fetch subject+body, call extract_email_facts.
         Skip if no shipment ID or invoice numbers found.
      3. For qualifying messages, run the board's agent via _execute_live_workflow.
      4. Persist the run record via _persist_agent_run.

    Log line at the end: seen / gated_in / gated_out / already_done counts.
    """
    # Import the shared primitives from run_live without modifying that file.
    from backend.api.run_live import _execute_live_workflow, _persist_agent_run  # type: ignore[attr-defined]

    log.info(
        "poll_once: tick started — boards=%s  query=%r  max_messages=%d",
        board_ids, query, max_messages,
    )

    message_ids = _fetch_recent_message_ids(query, max_messages)
    if not message_ids:
        log.info("poll_once: no messages found this tick")
        return

    gated_in = gated_out = already_done = 0

    for message_id in message_ids:

        # ── 1. Dedup pre-screen per board ─────────────────────────────────
        pending_boards = []
        for board_id in board_ids:
            if _already_processed(message_id, board_id):
                already_done += 1
                log.debug("poll_once: skip message=%s board=%s (already processed)", message_id, board_id)
            else:
                pending_boards.append(board_id)

        if not pending_boards:
            continue

        # ── 2. Shipment gate (one fetch for all pending boards) ────────────
        subject, body = _fetch_email_for_gate(message_id)
        if not _is_shipment_email(subject, body):
            gated_out += len(pending_boards)
            log.info(
                "poll_once: gated OUT message=%s subject=%r (not a shipment)",
                message_id, subject[:80],
            )
            continue

        gated_in += len(pending_boards)
        log.info(
            "poll_once: gated IN message=%s subject=%r boards=%s",
            message_id, subject[:80], pending_boards,
        )

        # ── 3 + 4. Run and persist for each board ─────────────────────────
        for board_id in pending_boards:
            try:
                run_data = await _execute_live_workflow(board_id, message_id)
                _persist_agent_run(board_id, message_id, run_data, status="completed")
                log.info(
                    "poll_once: ✓ completed  board=%s  message=%s  subject=%r",
                    board_id, message_id, run_data.get("subject", ""),
                )
                _maybe_email_result(run_data)
            except Exception as exc:
                log.error(
                    "poll_once: ✗ failed  board=%s  message=%s  error=%s",
                    board_id, message_id, exc, exc_info=True,
                )
                _persist_agent_run(
                    board_id, message_id,
                    {"subject": "", "csv_content": "", "result_json": {}},
                    status="failed",
                )

    log.info(
        "poll_once: tick done — seen=%d  gated_in=%d  gated_out=%d  already_done=%d",
        len(message_ids), gated_in, gated_out, already_done,
    )


# ── Main loop ──────────────────────────────────────────────────────────────────

async def main() -> None:
    interval   = int(os.environ.get("LISTENER_INTERVAL_SECONDS", "300"))
    max_msgs   = int(os.environ.get("LISTENER_MAX_MESSAGES", "20"))
    query      = os.environ.get("GMAIL_QUERY", "has:attachment")

    raw_boards = os.environ.get("LISTENER_BOARD_IDS", "")
    board_ids  = [b.strip() for b in raw_boards.split(",") if b.strip()]
    if not board_ids:
        board_ids = _discover_board_ids()

    if not board_ids:
        log.error(
            "main: no board IDs found — generate at least one agent before starting the listener"
        )
        return

    log.info(
        "Listener starting — interval=%ds  query=%r  boards=%s  max_messages=%d",
        interval, query, board_ids, max_msgs,
    )

    while True:
        try:
            await poll_once(query, board_ids, max_msgs)
        except Exception as exc:
            log.error("main: poll_once raised unexpectedly: %s", exc, exc_info=True)
        log.info("main: sleeping %ds until next tick", interval)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
