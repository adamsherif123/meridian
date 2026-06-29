"""Build-agent orchestration endpoint (Part C) + flow-state endpoint (Part D).

POST /api/v1/boards/{board_id}/build-agent
  Runs the full build sequence in the background:
    1. Freeze the spec
    2. Generate the agent (codegen)
    3. Eval against the captured worked example
    4. Self-heal loop (max 3 attempts) if eval doesn't pass
    5. Final status
  Returns immediately: {job_id, status: "running", poll_url}

GET /api/v1/boards/{board_id}/build-agent/status
  Returns current job state:
    {phase, attempt, message, done, result, job_id}
  Poll this until done=True.

GET /api/v1/boards/{board_id}/flow-state
  Returns the board's flow readiness so the UI can unlock steps:
    {ai_check_done, blocking_questions_open, worked_example_captured, agent_built}
"""

import asyncio
import json
import logging
import os
import pathlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from supabase import create_client

router = APIRouter(prefix="/api/v1/boards", tags=["build-agent"])
log = logging.getLogger(__name__)

MAX_HEAL_ATTEMPTS = 3

_CASES_DIR    = pathlib.Path(__file__).parent.parent / "evals" / "cases"
_AGENTS_DIR   = pathlib.Path(__file__).parent.parent / "agents" / "generated"

# ── In-memory job store ────────────────────────────────────────────────────────
# {board_id: {job_id, phase, attempt, message, done, result, started_at}}
# One active job per board (a new POST replaces the previous status).
_jobs: dict[str, dict] = {}


def _sb():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise HTTPException(503, "Supabase not configured")
    return create_client(url, key)


def _agent_file(board_id: str) -> pathlib.Path:
    safe_id = board_id.replace("-", "_")
    return _AGENTS_DIR / f"agent_{safe_id}.py"


def _find_case_file(board_id: str) -> pathlib.Path | None:
    """Find the eval case JSON for this board, preferring the worked-example file."""
    safe_id = board_id.replace("-", "")[:8]
    worked  = _CASES_DIR / f"board_{safe_id}_worked.json"
    if worked.exists():
        return worked
    # Fall back to any case file with matching board_id
    for path in _CASES_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("board_id") == board_id:
                return path
        except Exception:
            continue
    return None


def _set_phase(board_id: str, phase: str, message: str, attempt: int = 0) -> None:
    if board_id in _jobs:
        _jobs[board_id].update({"phase": phase, "message": message, "attempt": attempt})
    log.info("build-agent [%s] phase=%s attempt=%d  %s", board_id[:8], phase, attempt, message)


def _plain_field_failures(failed_ak: list[dict]) -> str:
    """Convert answer-key failures to a plain-language summary."""
    if not failed_ak:
        return ""
    parts = []
    for r in failed_ak[:4]:
        field   = r.get("field", "?")
        exp     = r.get("expected")
        actual  = r.get("actual")
        # Translate field names to plain language
        label_map = {
            "invoices_processed":  "invoices found",
            "invoices_succeeded":  "invoices passed",
            "invoices_failed":     "invoices failed",
            "goods_failed":        "goods checks failed",
            "batches_processed":   "batches found",
            "batches_succeeded":   "batches matched",
            "batches_failed":      "batches unmatched",
            "shipment_number":     "shipment number",
        }
        label = label_map.get(field, field.replace("_", " "))
        parts.append(f"{label} (expected {exp}, got {actual})")
    summary = ", ".join(parts)
    if len(failed_ak) > 4:
        summary += f", and {len(failed_ak) - 4} more"
    return summary


# ── Background build task ──────────────────────────────────────────────────────

async def _run_build(board_id: str, job_id: str) -> None:
    """Full build sequence: freeze → generate → eval → heal (max 3). Updates _jobs in place."""

    def _update(phase, message, attempt=0, done=False, result=None, error=None):
        _jobs[board_id].update({
            "phase":   phase,
            "message": message,
            "attempt": attempt,
            "done":    done,
            "result":  result,
            "error":   error,
        })
        log.info("build-agent [%s] phase=%s a=%d done=%s: %s", board_id[:8], phase, attempt, done, message)

    try:
        # Imports inside try so any ImportError surfaces as phase="failed" rather
        # than being swallowed silently by asyncio ("Task exception was never retrieved").
        from backend.api.gate import freeze_gate
        from backend.codegen.generate import codegen
        from backend.evals.evaluate import evaluate
        from backend.selfheal.heal import (
            _failed_ak_fields, _is_passing, _ak_pass_count,
            _ak_total_scored, _error_pass_count, _error_total,
        )

        # ── Step 1: Freeze the spec ────────────────────────────────────────────
        _update("freeze", "Reviewing and locking your process map…")
        try:
            freeze_result = await freeze_gate(board_id)
        except HTTPException as he:
            _update("failed", f"Your process map can't be locked yet — {he.detail}", done=True, error=he.detail)
            return
        except Exception as exc:
            _update("failed", f"Couldn't lock the process map: {exc}", done=True, error=str(exc))
            return

        # ── Step 2: Generate the agent ─────────────────────────────────────────
        _update("generate", "Writing your agent…")
        try:
            gen_result = await codegen(board_id)
            if gen_result.get("status") != "valid":
                errs = gen_result.get("validation_errors", [])
                msg  = f"Agent was generated but has errors: {errs[:2]}"
                _update("failed", msg, done=True, error=msg)
                return
        except HTTPException as he:
            _update("failed", f"Agent generation failed: {he.detail}", done=True, error=he.detail)
            return
        except Exception as exc:
            _update("failed", f"Agent generation failed: {exc}", done=True, error=str(exc))
            return

        # ── Step 3: Find the eval case ─────────────────────────────────────────
        case_path = _find_case_file(board_id)
        if case_path is None:
            _update(
                "done", "Agent built! No worked example found to test against — "
                "upload a worked example to verify accuracy.",
                done=True,
                result={"status": "built_no_eval", "validation": gen_result},
            )
            return

        # ── Step 4: Initial eval ───────────────────────────────────────────────
        _update("eval", "Trying it on your example…", attempt=0)
        try:
            eval_result = await evaluate(board_id, case_path=case_path)
        except Exception as exc:
            _update(
                "done", "Agent built! The test run hit an error — try running it manually.",
                done=True,
                result={"status": "built_eval_error", "error": str(exc)},
            )
            return

        if _is_passing(eval_result):
            _update(
                "done", "Your agent matches your example perfectly.",
                done=True,
                result={"status": "passed", "eval": _slim_eval(eval_result)},
            )
            return

        # ── Step 5: Heal loop (max MAX_HEAL_ATTEMPTS) ─────────────────────────
        for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
            failed_ak = _failed_ak_fields(eval_result)
            ak_pass   = _ak_pass_count(eval_result)
            ak_total  = _ak_total_scored(eval_result)
            cons_pass = _error_pass_count(eval_result)
            cons_total = _error_total(eval_result)

            if failed_ak:
                field_summary = _plain_field_failures(failed_ak)
                msg = f"It got {ak_pass} of {ak_total} fields right — fixing: {field_summary}…"
            else:
                msg = f"Fixing internal consistency issues (attempt {attempt})…"

            _update("heal", msg, attempt=attempt)

            try:
                # Run one heal attempt (use cap of 1 internal attempt per outer attempt)
                from backend.selfheal.heal import (
                    _agent_file as _af, _backup_path, _build_patch_goal,
                    _validate_agent_file, _load_attachment_filenames,
                    _done as _heal_done, _combined_score,
                )
                import shutil

                agent_file = _af(board_id)
                spec_result = await _load_frozen_spec_local(board_id)
                att_filenames = await _load_attachment_filenames(board_id)
                api_key = os.environ.get("ANTHROPIC_API_KEY", "")

                bak = _backup_path(agent_file, attempt)
                shutil.copy2(agent_file, bak)

                goal = _build_patch_goal(eval_result, agent_file, spec_result, att_filenames, attempt)

                from backend.selfheal.coding_agent import run_coding_agent
                _CONTRACT_PATH = pathlib.Path(__file__).parent.parent / "runtime" / "CONTRACT.md"

                agent_out = run_coding_agent(
                    goal=goal,
                    agent_file_path=agent_file,
                    contract_path=_CONTRACT_PATH,
                    api_key=api_key,
                )

                val_errors = _validate_agent_file(agent_file)
                if val_errors:
                    shutil.copy2(bak, agent_file)
                    _update(
                        "done",
                        f"Best effort after {attempt} attempt(s) — the patch had errors so we kept "
                        "the last good version. Your agent is ready to use.",
                        done=True,
                        result={"status": "best_effort", "attempts": attempt,
                                "eval": _slim_eval(eval_result), "validation_errors": val_errors[:3]},
                    )
                    return

                _update("eval", f"Checking again after fix {attempt}…", attempt=attempt)
                eval_result = await evaluate(board_id, case_path=case_path)

                if _is_passing(eval_result):
                    _update(
                        "done", "Fixed! Your agent now matches your example.",
                        done=True,
                        result={"status": "healed", "attempts": attempt, "eval": _slim_eval(eval_result)},
                    )
                    return

            except Exception as exc:
                log.warning("build-agent heal attempt %d failed: %s", attempt, exc, exc_info=True)
                _update(
                    "done",
                    f"Best effort after {attempt} attempt(s) — hit an unexpected error while fixing. "
                    "Your agent is ready to use but may need manual review.",
                    done=True,
                    result={"status": "best_effort", "attempts": attempt, "error": str(exc),
                            "eval": _slim_eval(eval_result)},
                )
                return

        # All heal attempts exhausted
        failed_ak = _failed_ak_fields(eval_result)
        if failed_ak:
            field_summary = _plain_field_failures(failed_ak)
            final_msg = (
                f"Best effort after {MAX_HEAL_ATTEMPTS} attempts — {field_summary} "
                "still differ from your example. Your agent is ready; check those fields manually."
            )
        else:
            final_msg = (
                f"Best effort after {MAX_HEAL_ATTEMPTS} attempts. "
                "Your agent is ready to use."
            )

        _update(
            "done", final_msg, attempt=MAX_HEAL_ATTEMPTS, done=True,
            result={"status": "best_effort", "attempts": MAX_HEAL_ATTEMPTS,
                    "eval": _slim_eval(eval_result)},
        )

    except Exception as exc:
        log.exception("build-agent background task crashed board_id=%s", board_id)
        _jobs[board_id].update({
            "phase": "failed",
            "message": f"Something went wrong: {exc}",
            "done": True,
            "error": str(exc),
        })


async def _load_frozen_spec_local(board_id: str) -> dict | None:
    """Load frozen spec for the heal loop (mirrors evaluate._load_frozen_spec)."""
    try:
        from supabase import create_client as _sc
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            return None
        sb = _sc(url, key)
        res = sb.table("frozen_specs").select("spec").eq("board_id", board_id).limit(1).execute()
        rows = (res.data or []) if res is not None else []
        return rows[0].get("spec") if rows else None
    except Exception:
        return None


def _slim_eval(eval_result: dict) -> dict:
    """Return a compact summary of the eval result for the job status payload."""
    return {
        "passed":   eval_result.get("passed"),
        "summary":  eval_result.get("summary"),
        "answer_key_fields": [
            {"field": r["field"], "expected": r["expected"],
             "actual": r["actual"], "passed": r["passed"]}
            for r in eval_result.get("answer_key", {}).get("field_results", [])
            if r.get("passed") is not None
        ],
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/{board_id}/build-agent")
async def build_agent(board_id: str) -> dict:
    """Start the build sequence: freeze → generate → eval → heal.

    Returns immediately with a job_id. Poll GET .../build-agent/status for progress.
    Plain-language progress messages are in the 'message' field.
    """
    job_id = str(uuid.uuid4())
    _jobs[board_id] = {
        "job_id":     job_id,
        "board_id":   board_id,
        "phase":      "starting",
        "attempt":    0,
        "message":    "Starting…",
        "done":       False,
        "result":     None,
        "error":      None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    # Fire and forget — FastAPI's background task mechanism
    asyncio.create_task(_run_build(board_id, job_id))

    return {
        "job_id":   job_id,
        "status":   "running",
        "poll_url": f"/api/v1/boards/{board_id}/build-agent/status",
    }


@router.get("/{board_id}/build-agent/status")
async def build_agent_status(board_id: str) -> dict:
    """Poll for progress on the build-agent job.

    Returns:
        {
            job_id:   str,
            phase:    "starting" | "freeze" | "generate" | "eval" | "heal" | "done" | "failed",
            attempt:  int,       — current heal attempt (0 if not healing)
            message:  str,       — plain-language description of current step
            done:     bool,      — True when the sequence is complete
            result:   dict|null, — final result when done=True
            error:    str|null,  — error message if failed
        }
    """
    job = _jobs.get(board_id)
    if job is None:
        raise HTTPException(404, "No build job found for this board. POST to /build-agent first.")
    return job


# ── Part D: flow-state endpoint ────────────────────────────────────────────────

@router.get("/{board_id}/flow-state")
async def flow_state(board_id: str) -> dict:
    """Return the board's guided-flow readiness so the UI can unlock steps.

    {
        ai_check_done:            bool  — gate has been run at least once,
        blocking_questions_open:  int   — open blocking comments remaining,
        worked_example_captured:  bool  — a worked-example eval case exists,
        agent_built:              bool  — a generated agent file exists on disk,
    }
    """
    try:
        sb = _sb()

        # ── ai_check_done + blocking_questions_open ────────────────────────────
        comments_res = (
            sb.table("gate_comments")
            .select("severity, status")
            .eq("board_id", board_id)
            .execute()
        )
        comments = comments_res.data or []
        ai_check_done = len(comments) > 0
        blocking_open = sum(
            1 for c in comments
            if c.get("severity") == "blocking"
            and c.get("status") not in ("resolved", "rejected")
        )

        # ── worked_example_captured ────────────────────────────────────────────
        safe_id   = board_id.replace("-", "")[:8]
        case_path = _CASES_DIR / f"board_{safe_id}_worked.json"
        worked_example_captured = case_path.exists()

        # Also check the evals/ cases directory for any case with this board_id
        if not worked_example_captured:
            for p in _CASES_DIR.glob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if data.get("board_id") == board_id:
                        worked_example_captured = True
                        break
                except Exception:
                    continue

        # ── agent_built ────────────────────────────────────────────────────────
        agent_built = _agent_file(board_id).exists()

        return {
            "ai_check_done":           ai_check_done,
            "blocking_questions_open": blocking_open,
            "worked_example_captured": worked_example_captured,
            "agent_built":             agent_built,
        }

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("flow_state failed board_id=%s", board_id)
        raise HTTPException(500, f"flow_state failed: {exc}")
