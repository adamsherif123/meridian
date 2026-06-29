"""ExampleAgentWorkflow — generic composition reference for S8 codegen.

NOT pharma-specific. Demonstrates:
  1. fetch_email_and_attachments (fixture mode in S7, real Gmail in S11)
  2. load_document from attachments
  3. validate_required_fields on the primary document
  4. FOR_EACH LOOP: validate each item in a provided list (scope node pattern)
  5. tally results across the loop
  6. emit_report

This is the shape S8 generates for real specs. The loop at step 4 corresponds
to a 'scope' node in the process map; S8 generates one loop per scope/for_each
node, nesting them when parentId indicates nested scopes.

Node-kind mapping illustrated here:
  tool_action(fetch)  → fetch_email_and_attachments
  expected_document   → load_document
  extract_validate    → validate_required_fields
  scope (for_each)    → Temporal for-loop (steps 4-4n)
  count               → tally
  report              → emit_report
"""
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from backend.runtime.activities.fetch_email import fetch_email_and_attachments
    from backend.runtime.activities.load_document import load_document
    from backend.runtime.activities.validate_fields import validate_required_fields
    from backend.runtime.activities.tally import tally
    from backend.runtime.activities.report import emit_report
    from backend.runtime.activities._types import (
        FetchEmailInput,
        LoadDocumentInput,
        ValidateFieldsInput,
        TallyInput,
        EmitReportInput,
    )

_TIMEOUT = timedelta(seconds=60)


@dataclass
class ExampleWorkflowInput:
    """Generic, spec-agnostic workflow input.

    In S8, codegen replaces these fields with values derived from the frozen spec.
    Field meanings correspond to spec node config fields (see CONTRACT.md).
    """
    # Email source (fixture in S7; live Composio Gmail in S11)
    email_message_id: str = "fixture-001"
    email_fixture_body: str = ""
    email_fixture_attachments: list[dict] = field(default_factory=list)

    # Primary document identification (maps to expected_document node config)
    document_identified_by: str = "filename"
    document_identifier: str = ""

    # Fields to validate on the primary document (maps to extract_validate node)
    document_fields: list[dict] = field(default_factory=list)
    document_fail_if: str = "any_missing"

    # For_each loop items (maps to scope node: iterate_over / item_name)
    items: list[dict] = field(default_factory=list)
    item_fields: list[dict] = field(default_factory=list)

    # Report config (maps to report node)
    report_format: str = "csv"
    report_columns: list[str] = field(default_factory=lambda: ["item", "passed", "fail_reason"])


@dataclass
class ExampleWorkflowResult:
    message_id: str
    document_name: str
    document_found: bool
    document_validation_passed: bool
    item_results: list[dict]   # [{item, passed, fail_reason}, ...]
    tally_counts: dict          # TallyResult.counts
    report_preview: str         # first 500 chars of emitted report


@workflow.defn
class ExampleAgentWorkflow:
    """Generic reference workflow — proves activity composition + for_each loop.

    S8 codegen generates real agent workflows that follow this exact pattern,
    with activity parameters filled from the frozen spec instead of input fields.
    """

    @workflow.run
    async def run(self, inp: ExampleWorkflowInput) -> ExampleWorkflowResult:

        # ── Step 1: Fetch email (fixture mode in S7) ────────────────────────
        # Maps to: tool_action node with action_type="fetch"
        email = await workflow.execute_activity(
            fetch_email_and_attachments,
            FetchEmailInput(
                message_id=inp.email_message_id,
                use_fixture=True,
                fixture_body=inp.email_fixture_body,
                fixture_attachments=inp.email_fixture_attachments,
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # ── Step 2: Load the primary document ───────────────────────────────
        # Maps to: expected_document node (identified_by / identifier from spec)
        doc = await workflow.execute_activity(
            load_document,
            LoadDocumentInput(
                attachments=email.attachments,
                identified_by=inp.document_identified_by,
                identifier=inp.document_identifier,
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # ── Step 3: Validate required fields on the document ─────────────────
        # Maps to: extract_validate node (fields, fail_if, applies_to from spec)
        doc_validation = await workflow.execute_activity(
            validate_required_fields,
            ValidateFieldsInput(
                document_text=doc.text,
                fields=inp.document_fields,
                fail_if=inp.document_fail_if,
                applies_to="per_document",
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # ── Step 4: FOR_EACH LOOP ────────────────────────────────────────────
        # Maps to: scope node (for_each) with iterate_over / item_name.
        # S8 generates one loop per scope node; nesting for nested scopes.
        item_results: list[dict] = []
        for item in inp.items:
            item_validation = await workflow.execute_activity(
                validate_required_fields,
                ValidateFieldsInput(
                    document_text=item.get("text", ""),
                    fields=inp.item_fields,
                    fail_if="any_missing",
                    applies_to="per_document",
                ),
                start_to_close_timeout=_TIMEOUT,
            )
            item_results.append({
                "item": item.get("name", ""),
                "passed": item_validation.passed,
                "fail_reason": item_validation.fail_reason,
            })

        # ── Step 5: Tally results ────────────────────────────────────────────
        # Maps to: count node (count_keys from spec)
        tally_result = await workflow.execute_activity(
            tally,
            TallyInput(
                results=item_results,
                count_keys=[{
                    "collection": "item",
                    "dedup_key": "item",
                    "label": "item_check",
                    "track": ["processed", "succeeded", "failed"],
                }],
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        # ── Step 6: Emit report ──────────────────────────────────────────────
        # Maps to: report node (format, columns from spec)
        report_result = await workflow.execute_activity(
            emit_report,
            EmitReportInput(
                format=inp.report_format,
                columns=inp.report_columns,
                rows=item_results,
            ),
            start_to_close_timeout=_TIMEOUT,
        )

        return ExampleWorkflowResult(
            message_id=email.message_id,
            document_name=doc.name,
            document_found=doc.found,
            document_validation_passed=doc_validation.passed,
            item_results=item_results,
            tally_counts=tally_result.counts,
            report_preview=report_result.content[:500],
        )
