"""App for the run-scratch-dir integration test (``test_run_scratch_dir.py``).

In its own module, not the test module, so that once #5056 (FND-5218) stops
``create_worker`` passing app modules through the workflow sandbox, Temporal
re-imports this module inside the sandbox for every run, the way it re-imports
a consumer app's module in production, and ``run()`` reads
``self.run_scratch_dir`` as sandboxed workflow code.
"""

import os

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.cleanup import CleanupInput


class ScratchInput(Input):
    workflow_dir: str = ""


class ScratchTaskOutput(Output):
    task_dir: str = ""
    wrote: str = ""


class ScratchOutput(Output):
    workflow_dir: str = ""
    task_dir: str = ""
    wrote: str = ""


class RunScratchDirApp(App):
    """Reads ``run_scratch_dir`` in workflow code and in a task of the same run."""

    name = "run-scratch-dir-app"

    @task
    async def write_scratch(self, input: ScratchInput) -> ScratchTaskOutput:
        # Write under the directory the *workflow* named, so a mismatch with
        # the task's own value would leave the file where cleanup never looks.
        os.makedirs(input.workflow_dir, exist_ok=True)
        path = os.path.join(input.workflow_dir, "out.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("scratch")
        return ScratchTaskOutput(task_dir=self.run_scratch_dir, wrote=path)

    async def run(self, input: ScratchInput) -> ScratchOutput:
        workflow_dir = self.run_scratch_dir
        result = await self.write_scratch(ScratchInput(workflow_dir=workflow_dir))
        return ScratchOutput(
            workflow_dir=workflow_dir, task_dir=result.task_dir, wrote=result.wrote
        )

    async def on_complete(self) -> None:
        # Only the local sweep: the integration env has no object store.
        await self.cleanup_files(CleanupInput())
