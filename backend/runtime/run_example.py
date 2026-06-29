"""Run ExampleAgentWorkflow with synthetic data.

Proves the activity library + Temporal scaffolding work end-to-end
with no pharma-specific or domain-specific values.

Prerequisites:
    1. temporal server start-dev           (Temporal dev server)
    2. python -m backend.runtime.worker    (activity worker)
    3. python -m backend.runtime.run_example  (this script)

All three run from the repo root with the backend venv activated.
"""
import asyncio
import base64
import json
import os
import uuid
from datetime import timedelta

from dotenv import load_dotenv
from temporalio.client import Client

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from backend.runtime.example_workflow import ExampleAgentWorkflow, ExampleWorkflowInput
from backend.runtime.worker import TASK_QUEUE


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# ── Synthetic fixtures — zero domain-specific values ──────────────────────────

_EMAIL_BODY = (
    "Notification: two documents are attached for processing."
)

_PRIMARY_DOC_TEXT = (
    "Reference Number: REF-001\n"
    "Quantity: 50 units\n"
    "Expiry Date: 2027-01-01\n"
    "Lot ID: LOT-XYZ\n"
)

_SYNTHETIC_ATTACHMENTS = [
    {
        "name": "primary_document.txt",
        "mime": "text/plain",
        "data_b64": _b64(_PRIMARY_DOC_TEXT),
    },
    {
        "name": "supplemental.txt",
        "mime": "text/plain",
        "data_b64": _b64("Supplemental: supplier code ABC\nContact: supplier@example.com"),
    },
]

_DOCUMENT_FIELDS = [
    {"name": "Reference Number", "appears_as": "", "scope": "document", "required": True},
    {"name": "Quantity",         "appears_as": "", "scope": "document", "required": True},
    {"name": "Expiry Date",      "appears_as": "", "scope": "document", "required": True},
]

# For-each items: three generic line items, one deliberately missing a required field
_SYNTHETIC_ITEMS = [
    {"name": "item-A", "text": "Lot ID: LOT-001\nQuantity: 10\nExpiry Date: 2026-06-01"},
    {"name": "item-B", "text": "Lot ID: LOT-002\nQuantity: 20\nExpiry Date: 2026-09-15"},
    {"name": "item-C", "text": "Lot ID: LOT-003\nQuantity: 5"},  # missing Expiry Date
]

_ITEM_FIELDS = [
    {"name": "Lot ID",      "required": True},
    {"name": "Expiry Date", "required": True},
]


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    workflow_id = f"example-agent-{uuid.uuid4()}"

    print("=" * 62)
    print("Meridian S7 — ExampleAgentWorkflow smoke run")
    print(f"  Temporal : {address}")
    print(f"  Queue    : {TASK_QUEUE}")
    print(f"  WF ID    : {workflow_id}")
    print("=" * 62)

    client = await Client.connect(address)

    inp = ExampleWorkflowInput(
        email_message_id="fixture-example-001",
        email_fixture_body=_EMAIL_BODY,
        email_fixture_attachments=_SYNTHETIC_ATTACHMENTS,
        document_identified_by="filename",
        document_identifier="primary_document",
        document_fields=_DOCUMENT_FIELDS,
        document_fail_if="any_missing",
        items=_SYNTHETIC_ITEMS,
        item_fields=_ITEM_FIELDS,
        report_format="csv",
        report_columns=["item", "passed", "fail_reason"],
    )

    result = await client.execute_workflow(
        ExampleAgentWorkflow.run,
        inp,
        id=workflow_id,
        task_queue=TASK_QUEUE,
        execution_timeout=timedelta(seconds=120),
    )

    print("\n── Result ──────────────────────────────────────────────────")
    print(f"  message_id              : {result.message_id}")
    print(f"  document_name           : {result.document_name}")
    print(f"  document_found          : {result.document_found}")
    print(f"  doc_validation_passed   : {result.document_validation_passed}")

    print(f"\n  Item results ({len(result.item_results)} items):")
    for r in result.item_results:
        mark = "✓" if r["passed"] else "✗"
        detail = f"  ({r['fail_reason']})" if not r["passed"] else ""
        print(f"    {mark} {r['item']}{detail}")

    print(f"\n  Tally:\n{json.dumps(result.tally_counts, indent=4)}")

    print(f"\n  Report preview ({len(result.report_preview)} chars):")
    print("  " + "\n  ".join(result.report_preview.splitlines()))

    print("\n✓ ExampleAgentWorkflow completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
