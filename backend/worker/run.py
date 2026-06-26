"""Temporal worker — polls task queue 'meridian-skeleton' for workflows and activities."""

import asyncio
import os

from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.worker import Worker

from backend.workflows.skeleton import SkeletonWorkflow
from backend.activities.checks import run_skeleton_checks

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(address)
    worker = Worker(
        client,
        task_queue="meridian-skeleton",
        workflows=[SkeletonWorkflow],
        activities=[run_skeleton_checks],
    )
    print(f"Worker started — task queue: meridian-skeleton  server: {address}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
