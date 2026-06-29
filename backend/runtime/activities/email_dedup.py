"""Email dedup guard activities.

Prevents double-processing of the same Gmail message.
Uses the processed_emails table (Supabase, service key — never exposed to browser).

SQL to create the table (paste into Supabase SQL editor):
  See supabase/schema.sql for the full DDL added in S7.
"""
import logging
import os
from datetime import datetime, timezone

from temporalio import activity

from backend.runtime.activities._types import EmailDedupInput, EmailDedupResult

log = logging.getLogger(__name__)


def _sb():
    from supabase import create_client  # lazy — only when activity is called
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_KEY not configured")
    return create_client(url, key)


@activity.defn
def is_email_processed(inp: EmailDedupInput) -> EmailDedupResult:
    """Check whether this Gmail message has already been processed.

    Inputs:
        inp.message_id: Gmail RFC-822 message-id (or fixture id for S7)
        inp.board_id: optional — scope the check to a specific board agent

    Outputs:
        EmailDedupResult(already_processed, marked_at)
        marked_at: ISO timestamp when first processed; empty if not yet processed.

    Fail-open: any error (network, missing table, etc.) returns already_processed=False
    so the workflow continues. For a fixture run this is always the right default.
    """
    log.info(
        "is_email_processed message_id=%s board_id=%r", inp.message_id, inp.board_id,
    )
    try:
        sb = _sb()
        # Use .limit(1) instead of .maybe_single()/.single() — those send an Accept
        # header requesting a single object and return HTTP 406 when zero rows match.
        # .limit(1) returns a plain list; empty list = not yet processed.
        query = (
            sb.table("processed_emails")
            .select("processed_at")
            .eq("message_id", inp.message_id)
            .limit(1)
        )
        if inp.board_id:
            query = query.eq("board_id", inp.board_id)
        res = query.execute()
        rows = (res.data or []) if res is not None else []
        if rows:
            return EmailDedupResult(
                already_processed=True,
                marked_at=rows[0].get("processed_at", ""),
            )
        return EmailDedupResult(already_processed=False)
    except Exception:
        log.warning(
            "is_email_processed: query failed for message_id=%s — failing open (not processed)",
            inp.message_id, exc_info=True,
        )
        return EmailDedupResult(already_processed=False)


@activity.defn
def mark_email_processed(inp: EmailDedupInput) -> EmailDedupResult:
    """Mark a Gmail message as processed to prevent double-execution.

    Uses upsert (on_conflict=ignore) so re-running fixtures never raises a
    unique-constraint error on (message_id, board_id).

    Inputs:
        inp.message_id: Gmail RFC-822 message-id
        inp.board_id: optional — scope the mark to a specific board agent

    Outputs:
        EmailDedupResult(already_processed=False, marked_at=<now>)

    Fail-open: any error is logged as a warning; the workflow continues regardless.
    """
    log.info(
        "mark_email_processed message_id=%s board_id=%r", inp.message_id, inp.board_id,
    )
    now = datetime.now(timezone.utc).isoformat()
    try:
        sb = _sb()
        row: dict = {"message_id": inp.message_id, "processed_at": now}
        if inp.board_id:
            row["board_id"] = inp.board_id
        # Upsert with ignoreDuplicates so fixture re-runs don't crash on the unique constraint.
        sb.table("processed_emails").upsert(row, ignore_duplicates=True).execute()
    except Exception:
        log.warning(
            "mark_email_processed: upsert failed for message_id=%s — continuing",
            inp.message_id, exc_info=True,
        )
    return EmailDedupResult(already_processed=False, marked_at=now)
