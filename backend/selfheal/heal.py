"""Self-heal orchestrator: runs the eval, invokes a coding agent to patch the
generated agent file on failure, re-validates, re-runs, and loops — with a
circuit-breaker (max attempts, revert on broken/regressed patch, stop on stall).

CLI:
    python -m backend.selfheal.heal <board_id>
    python -m backend.selfheal.heal <board_id> --case path/to/case.json

FastAPI endpoint:
    POST /api/v1/boards/{board_id}/heal

Circuit-breaker stops the loop on any of:
  MAX_ATTEMPTS   — 5 patch attempts without passing
  VALIDATION_FAIL — the coding agent produced a broken file (reverts, stops)
  REGRESSION      — fewer checks pass after a patch (reverts to best-known-good, stops)
  STALL           — STALL_LIMIT consecutive attempts with no improvement (stops)
"""

import asyncio
import json
import logging
import os
import pathlib
import shutil
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException

load_dotenv(dotenv_path=pathlib.Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

_REPO_ROOT     = pathlib.Path(__file__).parent.parent.parent
_AGENTS_DIR    = _REPO_ROOT / "backend" / "agents" / "generated"
_CONTRACT_PATH = _REPO_ROOT / "backend" / "runtime" / "CONTRACT.md"

MAX_ATTEMPTS = 5
STALL_LIMIT  = 2   # consecutive same-pass-count attempts before stopping

router = APIRouter(prefix="/api/v1/boards", tags=["selfheal"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _agent_file(board_id: str) -> pathlib.Path:
    safe_id = board_id.replace("-", "_")
    return _AGENTS_DIR / f"agent_{safe_id}.py"


def _backup_path(agent_file: pathlib.Path, attempt: int) -> pathlib.Path:
    return agent_file.with_suffix(f".bak{attempt}.py")


def _error_pass_count(eval_result: dict) -> int:
    checks = eval_result.get("consistency", {}).get("checks", [])
    return sum(1 for c in checks if c.get("severity") == "error" and c.get("passed"))


def _error_total(eval_result: dict) -> int:
    checks = eval_result.get("consistency", {}).get("checks", [])
    return sum(1 for c in checks if c.get("severity") == "error")


def _failed_error_checks(eval_result: dict) -> list[dict]:
    checks = eval_result.get("consistency", {}).get("checks", [])
    return [c for c in checks if c.get("severity") == "error" and not c.get("passed")]


def _is_passing(eval_result: dict) -> bool:
    return eval_result.get("passed", False)


def _ak_pass_count(eval_result: dict) -> int:
    """Number of scored answer-key fields that passed (placeholders excluded)."""
    frs = eval_result.get("answer_key", {}).get("field_results", [])
    return sum(1 for r in frs if r.get("passed") is True)


def _ak_total_scored(eval_result: dict) -> int:
    """Number of answer-key fields that are scored (not placeholders)."""
    frs = eval_result.get("answer_key", {}).get("field_results", [])
    return sum(1 for r in frs if r.get("passed") is not None)


def _failed_ak_fields(eval_result: dict) -> list[dict]:
    """Answer-key field_results that scored False (wrong, not placeholder)."""
    frs = eval_result.get("answer_key", {}).get("field_results", [])
    return [r for r in frs if r.get("passed") is False]


def _combined_score(eval_result: dict) -> int:
    """Combined pass count: consistency error checks + scored answer-key fields.

    Used for stall detection and regression checking so that improvement in
    answer-key correctness counts as progress even when consistency is maxed.
    Falls back to consistency-only when no answer key is present.
    """
    return _error_pass_count(eval_result) + _ak_pass_count(eval_result)


# ── Supabase helpers ───────────────────────────────────────────────────────────

async def _load_attachment_filenames(board_id: str) -> list[str]:
    """Load attachment filenames from sample_files — included in patch goal for diagnosis."""
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            return []
        sb = create_client(url, key)
        res = (
            sb.table("sample_files")
            .select("filename")
            .eq("board_id", board_id)
            .execute()
        )
        rows = (res.data or []) if res is not None else []
        return [r.get("filename", "") for r in rows if r.get("filename")]
    except Exception as exc:
        log.warning("_load_attachment_filenames: %s", exc)
        return []


def _persist_heal(board_id: str, heal_result: dict) -> None:
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            log.warning("Supabase not configured — heal_run not persisted")
            return
        sb = create_client(url, key)
        sb.table("heal_runs").insert({
            "board_id":   board_id,
            "status":     heal_result.get("status", "unknown"),
            "attempts":   heal_result.get("attempts", 0),
            "history":    heal_result.get("history", []),
            "final_eval": {
                k: v for k, v in heal_result.get("final_eval", {}).items()
                if k not in ("raw_csv",)   # keep jsonb small
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        log.info("heal_run persisted for board_id=%s status=%s", board_id, heal_result.get("status"))
    except Exception as exc:
        log.warning("_persist_heal failed: %s", exc, exc_info=True)


# ── Patch goal builder ─────────────────────────────────────────────────────────

def _build_patch_goal(
    eval_result: dict,
    agent_file: pathlib.Path,
    spec: dict | None,
    attachment_filenames: list[str],
    attempt: int,
) -> str:
    failed_checks = _failed_error_checks(eval_result)
    failed_ak     = _failed_ak_fields(eval_result)
    n_pass        = _error_pass_count(eval_result)
    n_total       = _error_total(eval_result)
    ak_pass_n     = _ak_pass_count(eval_result)
    ak_total      = _ak_total_scored(eval_result)

    # ── Consistency block ─────────────────────────────────────────────────────
    if failed_checks:
        checks_text = "\n".join(
            f"  [{c['name']}] {c['detail']}"
            for c in failed_checks
        )
        consistency_block = (
            f"Consistency failures ({len(failed_checks)} of {n_total} checks failing):\n"
            f"{checks_text}"
        )
    else:
        consistency_block = (
            f"Consistency checks: ALL {n_total} PASS — no consistency failures.\n"
            f"The agent output is internally consistent but SEMANTICALLY WRONG (see below)."
        )

    # ── Answer-key block ──────────────────────────────────────────────────────
    ak_block = ""
    if failed_ak:
        ak_lines = "\n".join(
            f"  {r['field']}: expected {r['expected']!r}, got {r['actual']!r}"
            for r in failed_ak
        )
        ak_block = (
            f"\nAnswer-key field mismatches — {len(failed_ak)} of {ak_total} scored fields wrong"
            f" (currently {ak_pass_n}/{ak_total} correct):\n"
            f"{ak_lines}\n"
            "\nThe agent produces internally consistent numbers that are factually incorrect.\n"
            "Diagnose the root cause: inspect the validate_required_fields 'appears_as' values,\n"
            "the match_by_key logic, and any document-parsing regexes in the agent. A field count\n"
            "being too low usually means a required label isn't found in the document text —\n"
            "check whether the appears_as string matches how the field actually appears in the\n"
            "document (it may be wrapped across lines, have a different prefix, or differ in\n"
            "spacing/punctuation vs what the agent searches for).\n"
        )

    # ── Attachment filenames block ─────────────────────────────────────────────
    filenames_block = ""
    if attachment_filenames:
        names = "\n".join(f"  - {n}" for n in attachment_filenames)
        filenames_block = f"\nActual attachment filenames in the test fixture:\n{names}\n"

    # ── Resolved assumptions block ─────────────────────────────────────────────
    resolved_block = ""
    if spec:
        resolved = [
            a for a in spec.get("resolved_assumptions", [])
            if a.get("status") in ("resolved", "answered")
        ]
        if resolved:
            lines = "\n".join(
                f"  Q: {a.get('question', '')}  →  A: {a.get('answer', '(no answer)')}"
                for a in resolved
            )
            resolved_block = f"\nResolved assumptions from the frozen spec (context only):\n{lines}\n"

    return f"""SELF-HEAL PATCH REQUEST — attempt {attempt}

The generated Temporal workflow at:
  {agent_file}

...is FAILING (combined signal: consistency {n_pass}/{n_total}, answer_key {ak_pass_n}/{ak_total}).

{consistency_block}
{ak_block}{filenames_block}{resolved_block}
Steps:
1. Read {agent_file}  — understand what it currently does.
2. Read {_CONTRACT_PATH}  — see the S7 activity signatures you must keep calling.
3. Identify the root cause of each failure above (consistency and/or answer-key).
4. Make the SMALLEST change that fixes those failures:
   - PREFER str_replace to replace only the buggy section (faster, no truncation risk).
   - Use write_file only if a large structural change is unavoidable.
5. After making the fix, STOP. The orchestrator will re-run the eval automatically.

CRITICAL CONSTRAINTS:
- Edit ONLY: {agent_file}
- Do NOT modify any S7 activity library file, eval harness, codegen, or other file.
- Keep all workflow.execute_activity() calls — same S7 activity functions + import paths.
- Fix must be MINIMAL and TARGETED — change only what causes the listed failures.
- The patched file must be valid Python.
"""


# ── Validation (reuse S8's checks) ────────────────────────────────────────────

def _validate_agent_file(agent_file: pathlib.Path) -> list[str]:
    """py_compile + S8 activity-signature AST walk. Returns [] if valid."""
    from backend.codegen.generate import _validate_code
    try:
        code = agent_file.read_text(encoding="utf-8")
        return _validate_code(code, agent_file)
    except Exception as exc:
        return [f"Validation exception: {exc}"]


# ── Result builders ────────────────────────────────────────────────────────────

def _make_result(
    status: str,
    board_id: str,
    attempts: int,
    history: list[dict],
    final_eval: dict,
    agent_file: pathlib.Path,
    **extra: object,
) -> dict:
    r = {
        "status":     status,
        "board_id":   board_id,
        "attempts":   attempts,
        "history":    history,
        "final_eval": final_eval,
        "agent_file": str(agent_file),
    }
    r.update(extra)
    return r


def _done(
    status: str,
    board_id: str,
    attempts: int,
    history: list[dict],
    final_eval: dict,
    agent_file: pathlib.Path,
    **extra: object,
) -> dict:
    result = _make_result(status, board_id, attempts, history, final_eval, agent_file, **extra)
    _persist_heal(board_id, result)
    return result


# ── Main orchestrator ──────────────────────────────────────────────────────────

async def heal(board_id: str, case_path: str | pathlib.Path | None = None) -> dict:
    """Self-heal loop: eval → patch → validate → re-eval → circuit-break → loop.

    Returns a structured result:
    {
        "status":     "healed" | "max_attempts" | "stalled" |
                      "revert_validation" | "revert_regression" | "agent_error",
        "board_id":   str,
        "attempts":   int,
        "history":    [ {attempt, eval_passed, pass_count, total_checks,
                          failed_checks, delta?, agent_turns?, regression?,
                          validation_errors?}, ... ],
        "final_eval": dict,   — last eval result (full consistency + answer_key)
        "agent_file": str,    — path to the (possibly patched) generated file
    }
    """
    from backend.evals.evaluate import evaluate, _find_case_file, _load_frozen_spec
    from backend.selfheal.coding_agent import run_coding_agent

    if case_path is None:
        case_path = _find_case_file(board_id)
    case_path = pathlib.Path(case_path)

    agent_file = _agent_file(board_id)
    if not agent_file.exists():
        raise FileNotFoundError(
            f"No generated agent at {agent_file}. "
            "Run POST /api/v1/boards/{board_id}/codegen first."
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    # Load supporting context (both fail-open)
    spec                = await _load_frozen_spec(board_id)
    attachment_filenames = await _load_attachment_filenames(board_id)

    history: list[dict] = []
    stall_count           = 0
    best_combined_score   = -1
    best_content: str | None = None

    log.info(
        "heal: start board_id=%s max_attempts=%d agent=%s",
        board_id, MAX_ATTEMPTS, agent_file,
    )

    # ── Initial eval ───────────────────────────────────────────────────────────
    eval_result = await evaluate(board_id, case_path=case_path)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        n_pass    = _error_pass_count(eval_result)
        n_total   = _error_total(eval_result)
        ak_pass_n = _ak_pass_count(eval_result)
        ak_total  = _ak_total_scored(eval_result)
        combined  = _combined_score(eval_result)   # consistency + answer-key
        failed    = _failed_error_checks(eval_result)
        failed_ak = _failed_ak_fields(eval_result)
        passing   = _is_passing(eval_result)

        log.info(
            "heal attempt %d: passing=%s  consistency %d/%d  answer_key %d/%d  combined=%d  failed=%s",
            attempt, passing, n_pass, n_total, ak_pass_n, ak_total, combined,
            [c["name"] for c in failed] + [r["field"] for r in failed_ak],
        )

        record: dict = {
            "attempt":       attempt,
            "eval_passed":   passing,
            "pass_count":    n_pass,
            "total_checks":  n_total,
            "failed_checks": [c["name"] for c in failed],
            "ak_pass_count": ak_pass_n,
            "ak_total":      ak_total,
            "combined":      combined,
        }

        # Delta based on combined score (consistency + answer-key) so that
        # answer-key progress counts even when consistency is already maxed.
        if len(history) > 0:
            prev_combined = history[-1].get("combined", history[-1]["pass_count"])
            record["delta"] = combined - prev_combined

        history.append(record)

        # ── SUCCESS ────────────────────────────────────────────────────────────
        if passing:
            log.info("heal: ALL CHECKS PASS on attempt %d — healed!", attempt)
            return _done("healed", board_id, attempt, history, eval_result, agent_file)

        # ── STALL check (only after first patch result) ────────────────────────
        if attempt > 1:
            delta = record.get("delta", 0)
            if delta == 0:
                stall_count += 1
                log.warning(
                    "heal: stall %d/%d (combined score unchanged at %d)",
                    stall_count, STALL_LIMIT, combined,
                )
                if stall_count >= STALL_LIMIT:
                    log.warning("heal: stall limit reached — stopping")
                    return _done("stalled", board_id, attempt, history, eval_result, agent_file)
            elif delta > 0:
                stall_count = 0   # improvement — reset stall counter

        # ── MAX ATTEMPTS (stop before patching on last allowed attempt) ────────
        if attempt >= MAX_ATTEMPTS:
            log.warning("heal: max attempts (%d) reached without passing", MAX_ATTEMPTS)
            return _done("max_attempts", board_id, attempt, history, eval_result, agent_file)

        # Track best-known-good content before we risk a bad patch (combined score)
        if combined > best_combined_score:
            best_combined_score = combined
            best_content        = agent_file.read_text(encoding="utf-8")

        # ── Backup before patching ─────────────────────────────────────────────
        bak = _backup_path(agent_file, attempt)
        shutil.copy2(agent_file, bak)
        log.info("heal: backup → %s", bak)

        # ── Build patch goal (includes answer-key discrepancies when present) ──
        goal = _build_patch_goal(eval_result, agent_file, spec, attachment_filenames, attempt)

        # ── Invoke coding agent ────────────────────────────────────────────────
        log.info("heal: invoking coding agent (attempt %d)", attempt)
        try:
            agent_out = run_coding_agent(
                goal=goal,
                agent_file_path=agent_file,
                contract_path=_CONTRACT_PATH,
                api_key=api_key,
            )
            record["agent_turns"] = agent_out.get("turns", 0)
            log.info(
                "heal: coding agent done success=%s turns=%d",
                agent_out.get("success"), agent_out.get("turns", 0),
            )
            if not agent_out.get("success"):
                log.warning("heal: coding agent failed: %s", agent_out.get("error", "unknown"))
        except Exception as exc:
            log.error("heal: coding agent raised: %s", exc, exc_info=True)
            shutil.copy2(bak, agent_file)
            log.info("heal: reverted to backup after agent error")
            return _done(
                "agent_error", board_id, attempt, history, eval_result, agent_file,
                error=str(exc),
            )

        # ── Validate the patched file ──────────────────────────────────────────
        val_errors = _validate_agent_file(agent_file)
        if val_errors:
            log.warning(
                "heal: patched file failed validation (%d errors) — reverting",
                len(val_errors),
            )
            shutil.copy2(bak, agent_file)
            log.info("heal: reverted to backup after validation failure")
            record["validation_errors"] = val_errors[:5]
            return _done(
                "revert_validation", board_id, attempt, history, eval_result, agent_file,
                validation_errors=val_errors,
            )

        # ── Re-run eval after patch ────────────────────────────────────────────
        eval_after       = await evaluate(board_id, case_path=case_path)
        combined_after   = _combined_score(eval_after)
        passing_after    = _is_passing(eval_after)

        log.info(
            "heal: after patch attempt %d: combined %d → %d  passing=%s",
            attempt, combined, combined_after, passing_after,
        )

        # ── REGRESSION check (combined score) ─────────────────────────────────
        if combined_after < best_combined_score:
            log.warning(
                "heal: REGRESSION — combined %d < best %d — reverting",
                combined_after, best_combined_score,
            )
            if best_content is not None:
                agent_file.write_text(best_content, encoding="utf-8")
                log.info(
                    "heal: reverted to best-known-good content (combined=%d)",
                    best_combined_score,
                )
            record["regression"] = True
            return _done(
                "revert_regression", board_id, attempt, history, eval_after, agent_file,
            )

        # Update tracking for next iteration
        if combined_after > best_combined_score:
            best_combined_score = combined_after
            best_content        = agent_file.read_text(encoding="utf-8")

        eval_result = eval_after   # feed forward into next loop iteration

        if passing_after:
            # SUCCESS detected right after the patch — add a final record
            log.info("heal: ALL CHECKS PASS after patch attempt %d — healed!", attempt)
            history.append({
                "attempt":       attempt + 1,
                "eval_passed":   True,
                "pass_count":    _error_pass_count(eval_after),
                "total_checks":  _error_total(eval_after),
                "failed_checks": [],
                "ak_pass_count": _ak_pass_count(eval_after),
                "ak_total":      _ak_total_scored(eval_after),
                "combined":      combined_after,
                "delta":         combined_after - combined,
            })
            return _done("healed", board_id, attempt + 1, history, eval_after, agent_file)

    # Should not reach here; guard
    return _done("max_attempts", board_id, MAX_ATTEMPTS, history, eval_result, agent_file)


# ── FastAPI endpoint ───────────────────────────────────────────────────────────

@router.post("/{board_id}/heal")
async def heal_endpoint(board_id: str) -> dict:
    """Trigger the self-heal loop for a board's generated agent.

    Runs synchronously (blocks until the loop finishes or circuit-breaks).
    Returns the heal result with status, attempts, history, and final eval.
    """
    try:
        return await heal(board_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(500, detail=str(exc))
    except Exception as exc:
        log.exception("heal_endpoint error for board_id=%s", board_id)
        raise HTTPException(500, detail=f"Heal loop error: {exc}")


# ── CLI output ─────────────────────────────────────────────────────────────────

def _print_result(result: dict) -> None:
    status    = result.get("status", "unknown")
    attempts  = result.get("attempts", 0)
    history   = result.get("history", [])
    final_eval = result.get("final_eval", {})

    sep = "─" * 72
    print()
    print(sep)
    print(f"  HEAL RESULT: {status.upper()}")
    print(f"  attempts: {attempts}  agent_file: {result.get('agent_file', '')}")
    print(sep)

    for rec in history:
        n    = rec["attempt"]
        p    = rec["pass_count"]
        t    = rec["total_checks"]
        ok   = rec["eval_passed"]
        ak_p = rec.get("ak_pass_count")
        ak_t = rec.get("ak_total")
        comb = rec.get("combined")
        delta_str = (
            f"  Δ{rec['delta']:+d}"
            if rec.get("delta") is not None
            else ""
        )
        reg = "  [REGRESSION]" if rec.get("regression") else ""

        # Build score string: show answer-key alongside consistency when present
        if ak_t:
            score_str = f"consistency {p}/{t}  answer_key {ak_p}/{ak_t}  combined={comb}"
        else:
            score_str = f"{p}/{t} pass"

        print(
            f"\n  Attempt {n}: {'PASSED ✓' if ok else f'FAILED  {score_str}'}"
            f"{delta_str}{reg}"
        )
        for name in rec.get("failed_checks", []):
            print(f"    ✗ consistency: {name}")
        if rec.get("agent_turns"):
            print(f"    coding agent turns: {rec['agent_turns']}")
        if rec.get("validation_errors"):
            print(f"    validation errors: {rec['validation_errors'][:2]}")

    print()

    # Print final consistency table
    checks = final_eval.get("consistency", {}).get("checks", [])
    if checks:
        print("  Final consistency checks:")
        for c in checks:
            mark = "✓" if c.get("passed") else "✗"
            sev  = "" if c.get("severity") == "error" else f"  [{c.get('severity','?')}]"
            print(f"    {mark} {c['name']:<40} {c.get('detail','')[:55]}{sev}")
    print()


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    _REPO_ROOT_STR = str(_REPO_ROOT)
    if _REPO_ROOT_STR not in sys.path:
        sys.path.insert(0, _REPO_ROOT_STR)

    parser = argparse.ArgumentParser(
        description="Self-heal loop: patch a generated agent until eval passes."
    )
    parser.add_argument("board_id", help="UUID of the board to heal")
    parser.add_argument(
        "--case",
        dest="case_path",
        default=None,
        help="Path to eval case JSON (default: auto-discovered from board_id)",
    )
    args = parser.parse_args()

    result = asyncio.run(heal(args.board_id, case_path=args.case_path))
    _print_result(result)

    # Exit 0 if healed, 1 otherwise
    sys.exit(0 if result.get("status") == "healed" else 1)
