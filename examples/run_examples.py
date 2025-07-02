"""
Utility functions for monitoring Temporal workflow execution status.
"""

import asyncio

from application_sdk.clients.utils import get_workflow_client
from application_sdk.test_utils.workflow_monitoring import run_and_monitor_workflow
from examples.application_hello_world import application_hello_world
from examples.application_sql import application_sql
from examples.application_sql_miner import application_sql_miner
from examples.application_sql_with_custom_transformer import (
    application_sql_with_custom_transformer,
)


async def main():
    workflow_client = get_workflow_client()
    await workflow_client.load()
    # run all the examples

    with open("workflow_status.md", "w", encoding="utf-8") as f:
        f.write("## 📦 Example workflows test results\n")
        f.write("- This workflow runs all the examples in the `examples` directory.\n")
        f.write("-----------------------------------\n")
        f.write("| Example | Status | Time Taken |\n")
        f.write("| --- | --- | --- |\n")

    examples = [
        application_sql,
        application_sql_with_custom_transformer,
        application_sql_miner,
        application_hello_world,
    ]

    failed_examples: list[str] = []

    for example in examples:
        status, time_taken = await run_and_monitor_workflow(example, workflow_client)
        time_taken_formatted = f"{time_taken:.2f} seconds"

        with open("workflow_status.md", "a", encoding="utf-8") as f:
            f.write(f"| {example.__name__} | {status} | {time_taken_formatted} |\n")

    with open("workflow_status.md", "a", encoding="utf-8") as f:
        f.write(
            "> This is an automatically generated file. Please do not edit directly.\n"
        )

    if failed_examples:
        raise Exception(f"Workflows {', '.join(failed_examples)} failed")


if __name__ == "__main__":
    asyncio.run(main())
