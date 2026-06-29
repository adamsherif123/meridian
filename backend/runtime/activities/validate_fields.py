"""validate_required_fields — Temporal activity.

PRESENCE check: is the field name (or its appears_as alias) visible in the document text?
Not a value extraction — consistent with the spec's intent for extract_validate nodes.
"""
import logging

from temporalio import activity

from backend.runtime.activities._types import (
    FieldSpec,
    ValidateFieldsInput,
    ValidateFieldsResult,
)

log = logging.getLogger(__name__)


def _search_term(spec: FieldSpec) -> str:
    """The string to search for: appears_as alias if set, else the field name."""
    alias = (spec.appears_as or "").strip()
    return alias if alias else spec.name


def _present_in(text: str, spec: FieldSpec) -> bool:
    return _search_term(spec).lower() in text.lower()


def _check_per_document(text: str, specs: list[FieldSpec]) -> list[dict]:
    return [
        {"name": s.name, "found": _present_in(text, s), "scope": "document"}
        for s in specs
    ]


def _check_per_line_item(text: str, specs: list[FieldSpec]) -> list[dict]:
    """For line-item scope: check whether each field appears in any non-empty line."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return [
        {
            "name": s.name,
            "found": any(_present_in(line, s) for line in lines),
            "scope": "line_item",
        }
        for s in specs
    ]


@activity.defn
def validate_required_fields(inp: ValidateFieldsInput) -> ValidateFieldsResult:
    """Check presence of declared fields in document text.

    PRESENCE only — does the field label / appears_as alias appear in the text?
    Value extraction is handled by downstream generated code, not here.

    Inputs:
        inp.document_text: full extracted text of the document
        inp.fields: list[FieldSpec-dict] {name, appears_as?, scope?, required?}
        inp.fail_if: "any_missing" | "all_missing" | "custom"
            "any_missing" — fail if ANY required field is absent
            "all_missing" — fail only if ALL required fields are absent
            "custom"      — always pass (calling workflow handles logic)
        inp.applies_to: "per_document" | "per_line_item"

    Outputs:
        ValidateFieldsResult(passed, field_results, fail_reason)
    """
    specs = [
        FieldSpec(
            name=f["name"],
            appears_as=f.get("appears_as", ""),
            scope=f.get("scope", "document"),
            required=f.get("required", True),
        )
        for f in inp.fields
    ]
    required = [s for s in specs if s.required]

    log.info(
        "validate_required_fields fields=%d required=%d fail_if=%s applies_to=%s",
        len(specs), len(required), inp.fail_if, inp.applies_to,
    )
    try:
        if inp.applies_to == "per_line_item":
            field_results = _check_per_line_item(inp.document_text, required)
        else:
            field_results = _check_per_document(inp.document_text, required)

        missing = [r for r in field_results if not r["found"]]

        if inp.fail_if == "any_missing":
            passed = len(missing) == 0
            fail_reason = f"Missing fields: {[r['name'] for r in missing]}" if missing else ""
        elif inp.fail_if == "all_missing":
            passed = len(missing) < len(required)
            fail_reason = "All required fields are missing" if not passed else ""
        else:  # "custom" — caller handles logic
            passed = True
            fail_reason = ""

        return ValidateFieldsResult(
            passed=passed,
            field_results=field_results,
            fail_reason=fail_reason,
        )
    except Exception as exc:
        log.warning(
            "validate_required_fields: unexpected error (fields=%d fail_if=%s) — returning failed result: %s",
            len(inp.fields), inp.fail_if, exc, exc_info=True,
        )
        return ValidateFieldsResult(
            passed=False,
            field_results=[],
            fail_reason=f"Internal error during field validation: {exc}",
        )


# ── Smoke test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    doc = "Invoice Number: 9001\nTotal Amount: $500\nCustomer: Acme"
    fields = [
        {"name": "Invoice Number", "required": True},
        {"name": "Total Amount",   "required": True},
        {"name": "Customer",       "required": True},
        {"name": "Tax Code",       "required": True},   # missing
    ]
    result = validate_required_fields(ValidateFieldsInput(
        document_text=doc, fields=fields, fail_if="any_missing",
    ))
    print("validate_required_fields OK")
    for r in result.field_results:
        mark = "✓" if r["found"] else "✗"
        print(f"  {mark} {r['name']}")
    print(f"  passed={result.passed} reason={result.fail_reason!r}")
    assert not result.passed
    assert "Tax Code" in result.fail_reason

    result2 = validate_required_fields(ValidateFieldsInput(
        document_text=doc,
        fields=[{"name": "Invoice Number", "required": True}],
        fail_if="any_missing",
    ))
    assert result2.passed
    print("  passing case OK")
