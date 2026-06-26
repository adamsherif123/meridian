"""FastAPI app — Temporal CLIENT that starts SkeletonWorkflow on each request."""

import asyncio
import os
import uuid
from datetime import timedelta

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from temporalio.client import Client  # noqa: E402 (after env load)

from backend.workflows.skeleton import SkeletonWorkflow  # noqa: E402
from backend.api.boards import router as boards_router  # noqa: E402

app = FastAPI(title="Meridian API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(boards_router)


@app.get("/api/v1/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/api/v1/skeleton/run")
async def run_skeleton() -> dict:
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    workflow_id = f"skeleton-{uuid.uuid4()}"
    try:
        client = await asyncio.wait_for(
            Client.connect(address),
            timeout=5.0,
        )
        result: dict = await client.execute_workflow(
            SkeletonWorkflow.run,
            "fastapi",
            id=workflow_id,
            task_queue="meridian-skeleton",
            execution_timeout=timedelta(seconds=60),
        )
        return {
            "temporal": {"status": "ok", "workflow_id": workflow_id},
            **result,
        }
    except asyncio.TimeoutError:
        return {
            "temporal": {
                "status": "error",
                "detail": f"Temporal server unreachable at {address} — run: temporal server start-dev",
            },
            "composio": {"status": "not_run", "detail": "Temporal not running"},
            "supabase": {"status": "not_run", "detail": "Temporal not running"},
        }
    except Exception as exc:
        return {
            "temporal": {"status": "error", "detail": str(exc)},
            "composio": {"status": "not_run", "detail": "Temporal not running"},
            "supabase": {"status": "not_run", "detail": "Temporal not running"},
        }
