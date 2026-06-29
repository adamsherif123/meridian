"""Temporal worker for the meridian-agent task queue.

Registers all reusable agent runtime activities + ExampleAgentWorkflow +
every generated agent workflow found in backend/agents/generated/.

Generated workflows are discovered dynamically on startup so newly generated
boards are available without restarting the worker or editing this file.

Usage:
    python -m backend.runtime.worker
"""
import asyncio
import importlib
import inspect
import logging
import os
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.worker import Worker

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

from backend.runtime.activities.classify_documents import classify_documents
from backend.runtime.activities.extract_email_facts import extract_email_facts
from backend.runtime.activities.fetch_email import fetch_email_and_attachments
from backend.runtime.activities.load_document import load_document
from backend.runtime.activities.validate_fields import validate_required_fields
from backend.runtime.activities.match_documents import match_by_key
from backend.runtime.activities.tally import tally
from backend.runtime.activities.report import emit_report, send_report
from backend.runtime.activities.email_dedup import is_email_processed, mark_email_processed
from backend.runtime.example_workflow import ExampleAgentWorkflow

log = logging.getLogger(__name__)

TASK_QUEUE = "meridian-agent"

_GENERATED_DIR = pathlib.Path(__file__).parent.parent / "agents" / "generated"

ALL_ACTIVITIES = [
    classify_documents,
    extract_email_facts,
    fetch_email_and_attachments,
    load_document,
    validate_required_fields,
    match_by_key,
    tally,
    emit_report,
    send_report,
    is_email_processed,
    mark_email_processed,
]


def _discover_generated_workflows() -> list:
    """Import every agent_*.py in backend/agents/generated/ and return their workflow classes.

    Skips backup files (names containing '.bak').  Logs and continues on any
    import error so a single bad generated file does not block the worker.
    """
    # Ensure repo root is on sys.path so backend.* imports work.
    repo_root = str(_GENERATED_DIR.parent.parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    found: list = []
    for path in sorted(_GENERATED_DIR.glob("agent_*.py")):
        if ".bak" in path.name:
            continue  # skip heal backups
        stem      = path.stem                                       # e.g. agent_1d44ee9c_...
        dotted    = f"backend.agents.generated.{stem}"
        sys.modules.pop(dotted, None)                               # always load fresh
        try:
            module = importlib.import_module(dotted)
        except Exception as exc:
            log.warning("worker: skipping %s — import failed: %s", path.name, exc)
            continue

        # Find the @workflow.defn class in the module
        wf_cls = None
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if hasattr(obj, "__temporal_workflow_definition"):
                wf_cls = obj
                break

        if wf_cls is None:
            log.warning("worker: no @workflow.defn class in %s — skipping", path.name)
            continue

        log.info("worker: discovered generated workflow %s ← %s", wf_cls.__name__, path.name)
        found.append(wf_cls)

    return found


async def main() -> None:
    address  = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    client   = await Client.connect(address)

    generated = _discover_generated_workflows()
    all_workflows = [ExampleAgentWorkflow] + generated

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=all_workflows,
        activities=ALL_ACTIVITIES,
        activity_executor=ThreadPoolExecutor(max_workers=10),
    )
    print(f"Worker started  task_queue={TASK_QUEUE}  server={address}")
    print(f"Workflows ({len(all_workflows)}): {[w.__name__ for w in all_workflows]}")
    print(f"Activities: {[fn.__name__ for fn in ALL_ACTIVITIES]}")
    print("Waiting for workflows…  (Ctrl-C to stop)")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
