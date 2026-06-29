"""General consistency checks for generated agent output.

Grades agent output without a ground-truth answer key.
Derives check structure from the output's own shape + the frozen spec (when available).
No domain-specific values are hardcoded here.

Five check families:
  A. balance         — succeeded + failed == processed for each counted collection
  B. non_empty       — *_processed == 0 despite N attachments present is a bug
  C. well_formed     — CSV has ≥1 data row; columns match spec report node if available
  D. sane_ranges     — no negatives; succeeded/failed ≤ processed
  E. key_field       — the report's key identifier column is non-empty
"""
import csv
import io
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

_COUNT_SUFFIXES = ("_processed", "_succeeded", "_failed", "_count")


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class Check:
    name:     str
    passed:   bool
    detail:   str
    severity: str = "error"   # "error" | "warning"

    def to_dict(self) -> dict:
        return {
            "name":     self.name,
            "passed":   self.passed,
            "detail":   self.detail,
            "severity": self.severity,
        }


# ── Spec helpers ──────────────────────────────────────────────────────────────

def _report_columns_from_spec(spec: dict) -> list[str]:
    """Return the expected column list from the spec's first report node.

    Tries two shapes:
      - config.columns: ["col1", "col2", ...]
      - config.blocks[*]{kind:"column", name:"..."}
    """
    for node in spec.get("nodes", []):
        data = node.get("data", {})
        if data.get("kind") != "report":
            continue
        config = data.get("config", {})
        cols = config.get("columns", [])
        if cols:
            return list(cols)
        cols = [
            b.get("name", "")
            for b in config.get("blocks", [])
            if b.get("kind") == "column" and b.get("name")
        ]
        if cols:
            return cols
    return []


def _key_field_from_spec(spec: dict) -> str | None:
    """Return the key-field name from spec meta (key_field or keyField)."""
    meta = spec.get("meta", {})
    return meta.get("key_field") or meta.get("keyField") or None


# ── A: Balance ────────────────────────────────────────────────────────────────

def _check_balance(actual: dict) -> list[Check]:
    """For each {prefix}_processed/succeeded/failed triple: succeeded + failed == processed."""
    suffixes  = ("_processed", "_succeeded", "_failed")
    prefixes: set[str] = set()
    for k in actual:
        if k.endswith("_processed"):
            pfx = k[: -len("_processed")]
            if f"{pfx}_succeeded" in actual and f"{pfx}_failed" in actual:
                prefixes.add(pfx)

    if not prefixes:
        return [Check(
            name="balance",
            passed=True,
            detail="No processed/succeeded/failed triples in output — balance not applicable",
            severity="warning",
        )]

    checks: list[Check] = []
    for pfx in sorted(prefixes):
        proc = actual.get(f"{pfx}_processed", 0)
        succ = actual.get(f"{pfx}_succeeded", 0)
        fail = actual.get(f"{pfx}_failed",    0)
        try:
            proc_i = int(proc)
            total  = int(succ) + int(fail)
        except (TypeError, ValueError):
            checks.append(Check(
                name=f"balance.{pfx}",
                passed=False,
                detail=(
                    f"{pfx}: non-numeric counts — "
                    f"processed={proc!r} succeeded={succ!r} failed={fail!r}"
                ),
                severity="error",
            ))
            continue
        passed = total == proc_i
        checks.append(Check(
            name=f"balance.{pfx}",
            passed=passed,
            detail=(
                f"{pfx}: {succ} succeeded + {fail} failed = {total} "
                f"{'==' if passed else '≠'} {proc_i} processed"
            ),
            severity="error",
        ))
    return checks


# ── B: Non-empty when input present ──────────────────────────────────────────

def _check_non_empty(actual: dict, attachment_count: int) -> list[Check]:
    """*_processed == 0 despite attachments being present is a clear bug signal."""
    processed_fields = sorted(k for k in actual if k.endswith("_processed"))
    if not processed_fields:
        return []

    if attachment_count == 0:
        return [Check(
            name="non_empty",
            passed=True,
            detail="No attachments provided — zero processed counts are expected",
            severity="warning",
        )]

    checks: list[Check] = []
    for field in processed_fields:
        pfx = field[: -len("_processed")]
        val = actual.get(field)
        try:
            count = int(val)
        except (TypeError, ValueError):
            continue

        if count == 0:
            checks.append(Check(
                name=f"non_empty.{pfx}",
                passed=False,
                detail=(
                    f"{field} = 0 despite {attachment_count} attachment(s) present. "
                    f"Likely a filename-filter or document-load bug in the generated workflow."
                ),
                severity="error",
            ))
        else:
            checks.append(Check(
                name=f"non_empty.{pfx}",
                passed=True,
                detail=f"{field} = {count} (non-zero with {attachment_count} attachment(s) present)",
                severity="error",
            ))
    return checks


# ── C: Well-formed output ─────────────────────────────────────────────────────

def _check_well_formed(raw_csv: str, actual: dict, spec: dict | None) -> list[Check]:
    """CSV has ≥1 data row; columns match spec's report node if spec available."""
    checks: list[Check] = []

    # C1: at least one data row
    try:
        all_rows = list(csv.reader(io.StringIO(raw_csv or "")))
        data_rows = max(0, len(all_rows) - 1)
    except Exception:
        data_rows = 0

    checks.append(Check(
        name="well_formed.has_data_row",
        passed=data_rows >= 1,
        detail=(
            f"CSV has {data_rows} data row(s)"
            if data_rows >= 1
            else "CSV is empty or header-only — no output was produced"
        ),
        severity="error",
    ))

    # C2: column set matches spec (only when spec is available)
    if spec:
        expected_cols = _report_columns_from_spec(spec)
        if expected_cols:
            actual_cols = set(actual.keys())
            exp_set     = set(expected_cols)
            missing     = sorted(exp_set - actual_cols)
            extra       = sorted(actual_cols - exp_set)
            col_passed  = not missing and not extra
            parts: list[str] = []
            if missing: parts.append(f"missing columns: {missing}")
            if extra:   parts.append(f"extra columns: {extra}")
            checks.append(Check(
                name="well_formed.columns_match_spec",
                passed=col_passed,
                detail=(
                    "All spec report columns present, no extras"
                    if col_passed
                    else "; ".join(parts)
                ),
                severity="error",
            ))

    return checks


# ── D: Sane ranges ────────────────────────────────────────────────────────────

def _check_sane_ranges(actual: dict) -> list[Check]:
    """No negative counts; succeeded/failed ≤ processed."""
    negatives = {
        k: v for k, v in actual.items()
        if isinstance(v, (int, float)) and v < 0
    }
    checks = [Check(
        name="sane_ranges.non_negative",
        passed=not negatives,
        detail="All counts ≥ 0" if not negatives else f"Negative values found: {negatives}",
        severity="error",
    )]

    violations: list[str] = []
    for field, val in actual.items():
        for sfx in ("_succeeded", "_failed"):
            if not field.endswith(sfx):
                continue
            pfx = field[: -len(sfx)]
            proc_key = f"{pfx}_processed"
            if proc_key not in actual:
                continue
            try:
                if int(val) > int(actual[proc_key]):
                    violations.append(
                        f"{field}={val} > {proc_key}={actual[proc_key]}"
                    )
            except (TypeError, ValueError):
                pass

    checks.append(Check(
        name="sane_ranges.counts_within_processed",
        passed=not violations,
        detail=(
            "All succeeded/failed counts ≤ processed"
            if not violations
            else f"Violations: {violations}"
        ),
        severity="error",
    ))
    return checks


# ── E: Key-field present ──────────────────────────────────────────────────────

def _check_key_field(actual: dict, spec: dict | None) -> list[Check]:
    """The report's identifying key field (e.g. shipment_number) must be non-empty.

    The spec's meta.key_field is a HUMAN LABEL (e.g. "Container or MAWB Number"),
    not necessarily the CSV column name (e.g. "shipment_number"). Only use the spec
    label directly if it matches an actual column key; otherwise fall back to the
    heuristic so we check the real output column, not a phantom one.
    """
    spec_label = _key_field_from_spec(spec) if spec else None

    # Use spec label only if it directly matches an output column name.
    # If it doesn't match, the label is a human description of the field, not the
    # column identifier — fall through to the heuristic.
    if spec_label and spec_label in actual:
        key_field = spec_label
    else:
        # Heuristic: first string-valued column that doesn't look like a count
        key_field = None
        for k, v in actual.items():
            if isinstance(v, str) and not any(k.endswith(s) for s in _COUNT_SUFFIXES):
                key_field = k
                break

    if key_field is None:
        return [Check(
            name="key_field.present",
            passed=True,
            detail="No key field identified in output — key-field check skipped",
            severity="warning",
        )]

    value   = actual.get(key_field, "")
    present = bool(value and str(value).strip())
    return [Check(
        name=f"key_field.{key_field}",
        passed=present,
        detail=(
            f"{key_field}={value!r} (present)"
            if present
            else f"{key_field} is empty or missing from the report output"
        ),
        severity="error",
    )]


# ── Public API ─────────────────────────────────────────────────────────────────

def run_checks(
    actual:           dict,
    raw_csv:          str,
    attachment_count: int,
    spec:             dict | None = None,
) -> list[Check]:
    """Run all consistency checks and return a list of Check results.

    General: derives structure from the output dict + frozen spec (when provided).
    No domain-specific values are hardcoded here.

    Args:
        actual:           Parsed CSV row as a flat dict (field → value).
        raw_csv:          The full CSV string from the agent's emit_report output.
        attachment_count: Number of fixture attachments given to the workflow.
        spec:             Frozen spec dict from Supabase (optional; enables richer checks).
    """
    checks: list[Check] = []
    checks.extend(_check_balance(actual))
    checks.extend(_check_non_empty(actual, attachment_count))
    checks.extend(_check_well_formed(raw_csv, actual, spec))
    checks.extend(_check_sane_ranges(actual))
    checks.extend(_check_key_field(actual, spec))
    return checks
