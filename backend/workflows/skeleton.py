"""SkeletonWorkflow — deterministic orchestration only; all I/O is in the activity."""

from datetime import timedelta
from temporalio import workflow

# imports_passed_through lets the sandbox load the activity reference without
# treating third-party imports inside activities/ as non-deterministic.
with workflow.unsafe.imports_passed_through():
    from backend.activities.checks import run_skeleton_checks


@workflow.defn
class SkeletonWorkflow:
    @workflow.run
    async def run(self, source: str) -> dict:
        return await workflow.execute_activity(
            run_skeleton_checks,
            source,
            start_to_close_timeout=timedelta(seconds=30),
        )
