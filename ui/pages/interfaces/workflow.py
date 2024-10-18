import asyncio
import os
from dataclasses import asdict

import pandas as pd
from sqlalchemy import create_engine
from temporalio.client import Client, WorkflowExecutionStatus


class WorkflowInterface(object):
    ENGINE = create_engine("sqlite:////tmp/app.db?mode=ro")

    def __init__(self, temporal_uri: str = "0.0.0.0:7233"):
        self.temporal_uri = temporal_uri

    @staticmethod
    def get_workflow_logs_df(
        workflow_id: str = None, run_id: str = None
    ) -> pd.DataFrame:
        if not workflow_id or not run_id:
            return pd.DataFrame([])

        return pd.read_sql(
            f"""
            SELECT
                severity,
                observed_timestamp,
                body,
                JSON_EXTRACT(attributes, '$.activity_type') as activity_type
            FROM logs
            WHERE JSON_EXTRACT(attributes, '$.workflow_id') = '{workflow_id}'
            AND JSON_EXTRACT(attributes, '$.run_id') = '{run_id}'
            ORDER BY observed_timestamp DESC
        """,
            con=WorkflowInterface.ENGINE,
        )

    async def fetch_workflows_list(self) -> list[dict]:
        client = await Client.connect(self.temporal_uri)
        workflows = client.list_workflows()
        workflow_list = []
        async for workflow in workflows:
            workflow_list.append(asdict(workflow))
        return workflow_list

    def fetch_workflows_df(self) -> (pd.DataFrame, dict):
        workflow_list = asyncio.run(self.fetch_workflows_list())
        df = pd.DataFrame(workflow_list)
        del df["data_converter"]
        del df["raw_info"]
        del df["typed_search_attributes"]

        df["run_time"] = df["close_time"] - df["execution_time"]
        df["run_time"] = df["run_time"].dt.total_seconds()
        df["run_id"] = (
            "["
            + df["run_id"]
            + "](http://localhost:8233/namespaces/default/workflows/"
            + df["id"]
            + "/"
            + df["run_id"]
            + "/history)"
        )
        df["status"] = df["status"].apply(lambda x: WorkflowExecutionStatus(x).name)

        df = df.filter(
            [
                "id",
                "status",
                "run_id",
                "workflow_type",
                "run_time",
                "execution_time",
                "start_time",
                "close_time",
                "task_queue",
            ]
        )

        column_defs = []
        for column in df.columns:
            if column == "run_id":
                column_defs.append(
                    {
                        "field": column,
                        "headerName": column,
                        "linkTarget": "_blank",
                        "cellRenderer": "markdown",
                    }
                )
            elif column == "status":
                column_defs.append(
                    {
                        "field": column,
                        "cellStyle": {
                            "styleConditions": [
                                {
                                    "condition": f"params.value == '{WorkflowExecutionStatus.RUNNING.name}'",
                                    "style": {
                                        "backgroundColor": "blue",
                                        "color": "white",
                                    },
                                },
                                {
                                    "condition": f"params.value == '{WorkflowExecutionStatus.COMPLETED.name}'",
                                    "style": {
                                        "backgroundColor": "green",
                                        "color": "white",
                                    },
                                },
                                {
                                    "condition": f"params.value == '{WorkflowExecutionStatus.FAILED.name}'",
                                    "style": {
                                        "backgroundColor": "red",
                                        "color": "white",
                                    },
                                },
                            ]
                        },
                    }
                )
            else:
                column_defs.append({"field": column})
        return df, column_defs

    @staticmethod
    def fetch_workflow_info(selected_rows: dict) -> (str, str):
        workflow_id = selected_rows[0]["id"]
        run_id_markdown = selected_rows[0]["run_id"]
        run_id = run_id_markdown[1 : run_id_markdown.index("]")]
        return workflow_id, run_id

    async def fetch_history(self, workflow_id: str, run_id: str) -> dict:
        client = await Client.connect(self.temporal_uri)
        history_obj = await client.get_workflow_handle(
            workflow_id, run_id=run_id
        ).fetch_history()
        return history_obj.to_json_dict()

    def fetch_workflow_events_df(self, workflow_id: str, run_id: str) -> pd.DataFrame:
        history = asyncio.run(self.fetch_history(workflow_id, run_id))
        return pd.DataFrame(history.get("events", []))

    @staticmethod
    def list_files(workflow_id: str, run_id: str) -> list[dict]:
        files = []
        base_path = f"/tmp/dapr/objectstore/{workflow_id}/{run_id}"
        for root, dirs, filenames in os.walk(base_path):
            if not filenames:
                continue

            file_dir = os.path.relpath(
                root, f"/tmp/dapr/objectstore/{workflow_id}/{run_id}"
            )
            for filename in filenames:
                files.append(
                    {
                        "dir": file_dir,
                        "filename": filename,
                        "full_path": os.path.join(root, filename),
                    }
                )
        return files

    def fetch_files_df(self, workflow_id: str, run_id: str) -> pd.DataFrame:
        files = self.list_files(workflow_id, run_id)
        return pd.DataFrame(files)
