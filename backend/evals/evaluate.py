"""Eval comparator, CLI, and optional FastAPI endpoint.

Runs the generated agent, then grades its output with TWO independent signals:
  1. Consistency checks (always — primary signal, no answer key needed).
  2. Answer-key comparison (secondary — only for fields with filled-in expected values).

CLI:
    python -m backend.evals.evaluate <board_id>
    python -m backend.evals.evaluate <board_id> --case path/to/case.json

Combined result shape (machine-readable — S10 self-heal consumes this):
    {
        "board_id":    "...",
        "case_name":   "...",
        "consistency": {
            "passed": false,
            "checks": [
                {"name": "balance.invoices",  "passed": true,  "detail": "...", "severity": "error"},
                {"name": "non_empty.invoices","passed": false, "detail": "invoices_processed = 0 despite 7 attachments...", "severity": "error"},
                ...
            ]
        },
        "answer_key": {
            "passed": false,          // null if all expected values are placeholders
            "field_results": [
                {"field": "invoices_processed", "expected": 3, "actual": 0, "passed": false, "note": ""},
                {"field": "invoices_succeeded",  "expected": null, "actual": 0, "passed": null,
                 "note": "placeholder — fill in expected value in case file"},
                ...
            ]
        },
        "passed":   false,
        "summary":  "consistency 5/8 · answer_key 1/2 · 6 placeholder(s) skipped",
        "raw_csv":  "shipment_number,...\\n..."
    }
"""
import asyncio
import json
import logging
import os
import pathlib
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

_CASES_DIR = pathlib.Path(__file__).parent / "cases"

router = APIRouter(prefix="/api/v1/boards", tags=["evals"])

_COL_FIELD    = 26
_COL_EXPECTED = 18
_COL_ACTUAL   = 18
_COL_CHECK    = 40


# ── Case file discovery ────────────────────────────────────────────────────────

def _find_case_file(board_id: str) -> pathlib.Path:
    """Scan cases/ for the JSON file whose board_id matches."""
    for path in _CASES_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("board_id") == board_id:
                return path
        except Exception:
            continue
    raise FileNotFoundError(
        f"No eval case file found for board_id={board_id} in {_CASES_DIR}. "
        f"Create backend/evals/cases/<name>.json with '\"board_id\": \"{board_id}\"'."
    )


# ── Frozen spec loader (optional — enriches consistency checks) ────────────────

async def _load_frozen_spec(board_id: str) -> dict | None:
    """Load the frozen spec for this board from Supabase. Returns None on any error."""
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            return None
        sb = create_client(url, key)
        res = (
            sb.table("frozen_specs")
            .select("spec")
            .eq("board_id", board_id)
            .limit(1)
            .execute()
        )
        rows = (res.data or []) if res is not None else []
        if rows:
            return rows[0].get("spec") or None
        return None
    except Exception as exc:
        log.warning("_load_frozen_spec failed (will skip spec-derived checks): %s", exc)
        return None


# ── Answer-key comparison ──────────────────────────────────────────────────────

def _coerce(value, reference):
    """Coerce value to the same type as reference for comparison."""
    if reference is None or value is None:
        return value
    try:
        if isinstance(reference, int):
            return int(value)
        if isinstance(reference, float):
            return float(value)
    except (ValueError, TypeError):
        pass
    return str(value)


def _compare_fields(expected: dict, actual: dict, tolerances: dict) -> list[dict]:
    """Compare each non-comment expected field against actual.

    Fields with expected=null are treated as placeholders (passed=None, not scored).
    """
    results: list[dict] = []
    for field, exp_val in expected.items():
        if field.startswith("_"):
            continue

        act_val = actual.get(field)

        if exp_val is None:
            results.append({
                "field":    field,
                "expected": None,
                "actual":   act_val,
                "passed":   None,
                "note":     "placeholder — fill in expected value in case file",
            })
            continue

        act_coerced = _coerce(act_val, exp_val)
        tolerance   = tolerances.get(field, 0)

        if isinstance(exp_val, (int, float)) and isinstance(act_coerced, (int, float)):
            field_passed = abs(act_coerced - exp_val) <= tolerance
        else:
            field_passed = str(act_coerced) == str(exp_val)

        results.append({
            "field":    field,
            "expected": exp_val,
            "actual":   act_coerced,
            "passed":   field_passed,
            "note":     "",
        })

    return results


# ── Supabase persistence ───────────────────────────────────────────────────────

def _persist_eval(board_id: str, eval_result: dict) -> None:
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            log.warning("Supabase not configured — eval result not persisted")
            return

        sb   = create_client(url, key)
        row  = {
            "board_id":      board_id,
            "case_name":     eval_result.get("case_name", ""),
            "passed":        eval_result.get("passed", False),
            "field_results": eval_result.get("answer_key", {}).get("field_results", []),
            "consistency":   eval_result.get("consistency", {}),
            "raw_csv":       eval_result.get("raw_csv", ""),
            "created_at":    datetime.now(timezone.utc).isoformat(),
        }
        try:
            sb.table("evals").upsert(row, on_conflict="board_id").execute()
            log.info("eval persisted (with consistency) for board_id=%s", board_id)
        except Exception as col_exc:
            # The 'consistency' column may not exist if the S9.1 ALTER hasn't been run.
            # Retry without it so the eval result is still stored.
            log.warning(
                "Persist with consistency column failed (%s) — "
                "run: ALTER TABLE evals ADD COLUMN IF NOT EXISTS consistency jsonb; "
                "Retrying without consistency...",
                col_exc,
            )
            row_slim = {k: v for k, v in row.items() if k != "consistency"}
            sb.table("evals").upsert(row_slim, on_conflict="board_id").execute()
            log.info("eval persisted (without consistency) for board_id=%s", board_id)
    except Exception as exc:
        log.warning("Failed to persist eval result: %s", exc, exc_info=True)


# ── Human-readable output ──────────────────────────────────────────────────────

def _print_table(eval_result: dict) -> None:
    name = eval_result.get("case_name", "")
    wf   = eval_result.get("workflow_class", "")
    csv_ = (eval_result.get("raw_csv") or "").strip()

    print()
    print(f"  Eval: {name}")
    if wf:
        print(f"  Workflow: {wf}")

    # ── Section 1: Consistency checks ─────────────────────────────────────
    print()
    print("  ── Consistency Checks ──────────────────────────────────────────────────────")
    checks = eval_result.get("consistency", {}).get("checks", [])
    if not checks:
        print("  (no checks run)")
    else:
        check_hdr = f"  {'Check':<{_COL_CHECK}} Pass  Detail"
        check_sep = "  " + "─" * 80
        print(check_hdr)
        print(check_sep)
        for c in checks:
            mark   = "✓" if c["passed"] else ("·" if c.get("severity") == "warning" else "✗")
            detail = (c.get("detail") or "")
            # Wrap long details at 60 chars on subsequent lines
            detail_first = detail[:72]
            print(f"  {c['name']:<{_COL_CHECK}} {mark}     {detail_first}")
            for chunk in [detail[i:i+72] for i in range(72, len(detail), 72)]:
                print(f"  {'':<{_COL_CHECK}}       {chunk}")
        print(check_sep)

    cons       = eval_result.get("consistency", {})
    all_checks = cons.get("checks", [])
    err_checks = [c for c in all_checks if c.get("severity") == "error"]
    err_pass_n = sum(1 for c in err_checks if c.get("passed"))
    c_overall  = cons.get("passed", False)
    print(
        f"  {'✓ PASS' if c_overall else '✗ FAIL'}  —  "
        f"{err_pass_n}/{len(err_checks)} error-severity checks passed"
    )

    # ── Section 2: Answer-key comparison ──────────────────────────────────
    print()
    print("  ── Answer-Key Comparison ───────────────────────────────────────────────────")
    fr = eval_result.get("answer_key", {}).get("field_results", [])
    if not fr:
        print("  (no answer-key expected values defined)")
    else:
        hdr = (
            f"  {'Field':<{_COL_FIELD}} "
            f"{'Expected':<{_COL_EXPECTED}} "
            f"{'Actual':<{_COL_ACTUAL}} "
            f"Pass"
        )
        sep = "  " + "─" * (len(hdr) - 2)
        print(hdr)
        print(sep)
        for r in fr:
            exp_disp = "—" if r["expected"] is None else str(r["expected"])
            act_disp = "—" if r["actual"]   is None else str(r["actual"])
            if r["passed"] is None:
                mark = "·"
            elif r["passed"]:
                mark = "✓"
            else:
                mark = "✗"
            print(
                f"  {r['field']:<{_COL_FIELD}} "
                f"{exp_disp:<{_COL_EXPECTED}} "
                f"{act_disp:<{_COL_ACTUAL}} "
                f"{mark}"
            )
        print(sep)
        ak        = eval_result.get("answer_key", {})
        ak_pass   = ak.get("passed")
        scored    = [r for r in fr if r.get("passed") is not None]
        pass_n    = sum(1 for r in scored if r.get("passed"))
        if scored:
            ak_mark = "✓ PASS" if ak_pass else "✗ FAIL"
            print(f"  {ak_mark}  —  {pass_n}/{len(scored)} fields correct")
        else:
            print("  (all fields are placeholders — fill in expected values to enable scoring)")

    # ── Overall ────────────────────────────────────────────────────────────
    print()
    overall_mark = "✓ OVERALL PASS" if eval_result.get("passed") else "✗ OVERALL FAIL"
    print(f"  {overall_mark}  —  {eval_result.get('summary', '')}")
    print()

    if csv_:
        print("  Raw CSV:")
        for line in csv_.splitlines():
            print(f"    {line}")
        print()


# ── Core evaluation logic ──────────────────────────────────────────────────────

async def evaluate(board_id: str, case_path: str | pathlib.Path | None = None) -> dict:
    """Run the generated agent and grade output with consistency + answer-key signals.

    Returns a combined structured result consumed by S10 self-heal.
    """
    from backend.evals.runner import run_eval
    from backend.evals.consistency import run_checks as run_consistency_checks

    if case_path is None:
        case_path = _find_case_file(board_id)
    else:
        case_path = pathlib.Path(case_path)

    case       = json.loads(case_path.read_text(encoding="utf-8"))
    case_name  = case.get("name", str(case_path.stem))
    expected   = {k: v for k, v in case.get("expected", {}).items()}
    tolerances = case.get("tolerances", {})

    log.info("evaluate: board_id=%s case=%s", board_id, case_name)

    # ── Run the agent ──────────────────────────────────────────────────────
    run_result       = await run_eval(board_id, case_path)
    actual           = run_result["actual"]
    raw_csv          = run_result["raw_csv"]
    attachment_count = run_result.get("attachment_count", 0)

    # ── Load frozen spec (optional — enables richer consistency checks) ────
    spec = await _load_frozen_spec(board_id)
    if spec:
        log.info("evaluate: frozen spec loaded (%d nodes)", len(spec.get("nodes", [])))
    else:
        log.info("evaluate: no frozen spec — consistency checks will use heuristics only")

    # ── Signal 1: Consistency checks (primary, no answer key needed) ───────
    checks       = run_consistency_checks(actual, raw_csv, attachment_count, spec)
    err_checks   = [c for c in checks if c.severity == "error"]
    cons_passed  = all(c.passed for c in err_checks)
    err_pass_n   = sum(1 for c in err_checks if c.passed)

    consistency = {
        "passed": cons_passed,
        "checks": [c.to_dict() for c in checks],
    }

    # ── Signal 2: Answer-key comparison (secondary, requires filled expected) ──
    field_results = _compare_fields(expected, actual, tolerances)
    scored        = [r for r in field_results if r["passed"] is not None]
    placeholder_n = len(field_results) - len(scored)
    ak_passed     = (all(r["passed"] for r in scored) if scored else None)
    ak_pass_n     = sum(1 for r in scored if r["passed"])

    answer_key = {
        "passed":        ak_passed,
        "field_results": field_results,
    }

    # ── Overall ────────────────────────────────────────────────────────────
    # Consistency (error-severity checks) must pass.
    # Answer-key also must pass if any fields are scored.
    overall = cons_passed and (ak_passed is None or ak_passed)

    parts = [f"consistency {err_pass_n}/{len(err_checks)}"]
    if scored:
        parts.append(f"answer_key {ak_pass_n}/{len(scored)}")
    if placeholder_n:
        parts.append(f"{placeholder_n} placeholder(s) skipped")
    summary = " · ".join(parts)

    eval_result = {
        "board_id":       board_id,
        "case_name":      case_name,
        "consistency":    consistency,
        "answer_key":     answer_key,
        "passed":         overall,
        "summary":        summary,
        "raw_csv":        raw_csv,
        "workflow_class": run_result.get("workflow_class", ""),
        "message_id":     run_result.get("message_id", ""),
    }

    _persist_eval(board_id, eval_result)
    return eval_result


# ── FastAPI endpoint ───────────────────────────────────────────────────────────

@router.post("/{board_id}/eval")
async def eval_endpoint(board_id: str, case_file: str | None = None) -> dict:
    """Run the eval harness for a board and return the combined structured result.

    Query params:
        case_file: path to a case JSON file; auto-discovered from cases/ if omitted.

    Returns: consistency checks + answer-key comparison + overall pass/fail.
    S10 self-heal reads this to determine what to fix.
    """
    log.info("eval_endpoint board_id=%s case_file=%r", board_id, case_file)
    try:
        result = await evaluate(board_id, case_path=case_file)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        log.exception("eval_endpoint failed board_id=%s", board_id)
        raise HTTPException(status_code=500, detail=f"Eval failed: {exc}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Run the eval harness for a generated board agent."
    )
    parser.add_argument("board_id", help="UUID of the board to evaluate")
    parser.add_argument(
        "--case", dest="case_file", default=None,
        help="Path to the eval case JSON file (auto-discovered from cases/ if omitted)",
    )
    args = parser.parse_args()

    try:
        result = asyncio.run(evaluate(args.board_id, case_path=args.case_file))
        _print_table(result)
        sys.exit(0 if result["passed"] else 1)
    except FileNotFoundError as exc:
        print(f"\n  ERROR: {exc}\n", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        log.exception("eval failed")
        print(f"\n  ERROR: {exc}\n", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
