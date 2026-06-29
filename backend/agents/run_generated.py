"""Run the generated agent workflow for a board against sample file fixtures.

Discovers the generated workflow class, starts a short-lived Temporal worker,
triggers the workflow with board's sample_files as fixture inputs, and prints results.

Usage (from repo root, with Temporal dev server running):
    temporal server start-dev          # terminal 1
    python -m backend.agents.run_generated <board_id>  # terminal 2

The worker for the generated workflow is embedded in this runner; no separate
worker process needed.
"""
import asyncio
import base64
import importlib
import inspect
import json
import logging
import os
import pathlib
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields as dc_fields, is_dataclass
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger(__name__)

_AGENTS_DIR = pathlib.Path(__file__).parent / "generated"

# ── Module loading ────────────────────────────────────────────────────────────

def _agent_filepath(board_id: str) -> pathlib.Path:
    safe_id = board_id.replace("-", "_")
    return _AGENTS_DIR / f"agent_{safe_id}.py"


def _load_generated_module(board_id: str):
    """Import the generated agent module by its real dotted package path.

    Uses importlib.import_module("backend.agents.generated.agent_<safe_id>") so
    Temporal's sandbox can re-import the module by the same dotted name without
    ModuleNotFoundError (the sandbox calls importlib.import_module(mod.__name__)
    internally to enforce determinism; a synthetic name from spec_from_file_location
    is not findable that way).
    """
    filepath = _agent_filepath(board_id)
    if not filepath.exists():
        raise FileNotFoundError(
            f"No generated agent found at {filepath}. "
            f"Run POST /api/v1/boards/{board_id}/codegen first."
        )

    # Ensure the repo root is on sys.path so backend.* is importable when this
    # script is invoked directly (python -m already does this; direct invocation may not).
    repo_root = str(pathlib.Path(__file__).parent.parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    safe_id = board_id.replace("-", "_")
    module_dotted = f"backend.agents.generated.agent_{safe_id}"

    # Evict stale cache entry so a regenerated file always loads fresh.
    sys.modules.pop(module_dotted, None)

    module = importlib.import_module(module_dotted)
    log.info("Loaded generated module: %s", module_dotted)
    return module


def _find_workflow_class(module):
    """Find the @workflow.defn class in the generated module."""
    from temporalio import workflow as _wf
    for name, obj in inspect.getmembers(module, inspect.isclass):
        # Temporal marks classes with __temporal_workflow_definition after @workflow.defn
        if hasattr(obj, "__temporal_workflow_definition"):
            log.info("Found workflow class: %s", name)
            return obj
    raise RuntimeError(
        f"No @workflow.defn class found in generated module. "
        f"Check the generated file for errors."
    )


def _find_input_class(workflow_cls):
    """Find the workflow's run() input dataclass via type annotation."""
    run_method = getattr(workflow_cls, "run", None)
    if run_method is None:
        raise RuntimeError(f"Workflow class {workflow_cls.__name__} has no run() method")
    hints = {}
    try:
        import typing
        hints = typing.get_type_hints(run_method)
    except Exception:
        pass
    # run(self, inp: <InputClass>) → first non-self, non-return hint
    params = list(inspect.signature(run_method).parameters.keys())
    if len(params) < 2:
        raise RuntimeError("run() has fewer than 2 parameters (expected self + input)")
    inp_param_name = params[1]  # skip 'self'
    inp_cls = hints.get(inp_param_name)
    if inp_cls is None or not is_dataclass(inp_cls):
        raise RuntimeError(
            f"Could not find input dataclass for run() parameter '{inp_param_name}'. "
            f"Type hints: {hints}"
        )
    log.info("Input class: %s", inp_cls.__name__)
    return inp_cls


# ── Fixture loading from Supabase sample_files ────────────────────────────────

async def _load_sample_fixtures(board_id: str) -> list[dict]:
    """Load sample_files for this board from Supabase storage → Attachment-dicts."""
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            log.warning("Supabase not configured — using empty fixture attachments")
            return []

        sb = create_client(url, key)
        files_res = (
            sb.table("sample_files")
            .select("filename, mime, storage_path, extracted_text")
            .eq("board_id", board_id)
            .execute()
        )
        rows = files_res.data or []
        if not rows:
            log.info("No sample_files found for board %s — using empty fixtures", board_id)
            return []

        attachments = []
        for row in rows:
            extracted_text = row.get("extracted_text", "") or ""
            # Pass extracted text rather than raw bytes so the workflow input stays
            # well within Temporal's blob-size limit (~few MB). load_document's
            # text/plain branch decodes this directly — no pdfplumber needed.
            data_b64 = base64.b64encode(extracted_text.encode()).decode() if extracted_text else ""
            storage_path  = row.get("storage_path", "") or ""
            original_mime = row.get("mime", "") or ""
            log.info(
                "Fixture: %s  text=%d chars  storage_path=%s",
                row.get("filename", "?"), len(extracted_text), storage_path or "(none)",
            )
            attachments.append({
                "name":          row.get("filename", "attachment"),
                "mime":          "text/plain",   # overridden to stay within Temporal blob limit
                "data_b64":      data_b64,
                "storage_path":  storage_path,   # enables vision fallback in load_document
                "original_mime": original_mime,  # real MIME for vision block type selection
            })

        log.info("Loaded %d fixture attachments for board %s", len(attachments), board_id)
        return attachments

    except Exception as exc:
        log.warning("Failed to load sample fixtures: %s — using empty list", exc)
        return []


# ── Worker + runner ───────────────────────────────────────────────────────────

async def run_generated_agent(board_id: str) -> None:
    from temporalio.client import Client
    from temporalio.worker import Worker

    # Import all S7 activities
    from backend.runtime.activities.classify_documents import classify_documents
    from backend.runtime.activities.extract_email_facts import extract_email_facts
    from backend.runtime.activities.fetch_email import fetch_email_and_attachments
    from backend.runtime.activities.load_document import load_document
    from backend.runtime.activities.validate_fields import validate_required_fields
    from backend.runtime.activities.match_documents import match_by_key
    from backend.runtime.activities.tally import tally
    from backend.runtime.activities.report import emit_report, send_report
    from backend.runtime.activities.email_dedup import is_email_processed, mark_email_processed
    from backend.runtime.worker import TASK_QUEUE

    ALL_ACTIVITIES = [
        classify_documents, extract_email_facts, fetch_email_and_attachments, load_document,
        validate_required_fields, match_by_key, tally, emit_report, send_report,
        is_email_processed, mark_email_processed,
    ]

    # Load generated workflow
    module = _load_generated_module(board_id)
    workflow_cls = _find_workflow_class(module)
    input_cls = _find_input_class(workflow_cls)

    # Load fixture attachments from Supabase sample_files
    fixture_attachments = await _load_sample_fixtures(board_id)

    # Build input: set use_fixture=True, provide sample files
    input_kwargs = {}
    if is_dataclass(input_cls):
        field_names = {f.name for f in dc_fields(input_cls)}
        if "message_id" in field_names:
            input_kwargs["message_id"] = f"fixture-run-{uuid.uuid4()}"
        if "use_fixture" in field_names:
            input_kwargs["use_fixture"] = True
        if "fixture_body" in field_names:
            input_kwargs["fixture_body"] = "Sample email body for fixture run."
        if "fixture_attachments" in field_names:
            input_kwargs["fixture_attachments"] = fixture_attachments
        if "board_id" in field_names:
            input_kwargs["board_id"] = board_id

    inp = input_cls(**input_kwargs)
    workflow_id = f"generated-{board_id}-{uuid.uuid4()}"

    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    print("=" * 62)
    print(f"Meridian S8 — generated agent run")
    print(f"  Board ID  : {board_id}")
    print(f"  Workflow  : {workflow_cls.__name__}")
    print(f"  Input     : {input_cls.__name__}")
    print(f"  Fixtures  : {len(fixture_attachments)} attachment(s)")
    print(f"  Temporal  : {address}")
    print(f"  WF ID     : {workflow_id}")
    print("=" * 62)

    client = await Client.connect(address)

    # Start worker (embedded in runner process)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[workflow_cls],
        activities=ALL_ACTIVITIES,
        activity_executor=ThreadPoolExecutor(max_workers=10),
    )

    # Run workflow and worker concurrently; stop worker when workflow completes
    async def _run_workflow():
        return await client.execute_workflow(
            workflow_cls.run,
            inp,
            id=workflow_id,
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=180),
        )

    async with worker:
        result = await _run_workflow()

    print("\n── Result ──────────────────────────────────────────────────")
    if is_dataclass(result):
        for f in dc_fields(result):
            val = getattr(result, f.name)
            if isinstance(val, (list, dict)):
                print(f"  {f.name}: {json.dumps(val, default=str, indent=2)[:300]}")
            else:
                print(f"  {f.name}: {val}")
    else:
        print(f"  {result}")
    print("\n✓ Generated agent workflow completed.")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m backend.agents.run_generated <board_id>")
        sys.exit(1)
    board_id = sys.argv[1]
    asyncio.run(run_generated_agent(board_id))


if __name__ == "__main__":
    main()
