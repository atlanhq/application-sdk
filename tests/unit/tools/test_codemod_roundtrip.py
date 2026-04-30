"""E2: Migration roundtrip test suite.

Four synthetic v2 connector fixtures (sql_metadata, handler, sql_query, custom)
run through the full codemod pipeline and assert structural correctness of the
output.  These tests verify the pipeline end-to-end without requiring a live
Temporal server.
"""

from __future__ import annotations

from pathlib import Path

from tools.migrate_v3.run_codemods import run_codemods


def _write(tmp_path: Path, filename: str, source: str) -> Path:
    p = tmp_path / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return p


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture 1: sql_metadata connector (activities + workflow + main)
# ---------------------------------------------------------------------------


class TestSqlMetadataRoundtrip:
    def _setup(self, tmp_path: Path) -> Path:
        root = tmp_path / "atlan-sqltest-app"
        _write(
            tmp_path,
            "atlan-sqltest-app/app/activities.py",
            "from temporalio import activity\n"
            "from application_sdk.activities import BaseSQLMetadataExtractionActivities\n"
            "from application_sdk.activities.common.models import ActivityStatistics\n\n"
            "class SqlTestActivities(BaseSQLMetadataExtractionActivities):\n"
            "    activities_cls = SqlTestActivities\n\n"
            "    @activity.defn\n"
            "    async def fetch_databases(self, workflow_args):\n"
            "        return ActivityStatistics(total_record_count=0)\n\n"
            "    @activity.defn\n"
            "    async def fetch_schemas(self, workflow_args):\n"
            "        return ActivityStatistics(total_record_count=0)\n",
        )
        _write(
            tmp_path,
            "atlan-sqltest-app/app/workflow.py",
            "from temporalio import workflow\n"
            "from temporalio.common import RetryPolicy\n\n"
            "from app.activities import SqlTestActivities\n\n"
            "@workflow.defn\n"
            "class SqlTestWorkflow:\n"
            "    activities_cls = SqlTestActivities\n\n"
            "    @staticmethod\n"
            "    def get_activities(activities):\n"
            "        return [activities.fetch_databases, activities.fetch_schemas]\n\n"
            "    @workflow.run\n"
            "    async def run(self, workflow_config):\n"
            "        activities_instance = self.activities_cls()\n"
            "        db = await workflow.execute_activity_method(\n"
            "            activities_instance.fetch_databases,\n"
            "            args=[workflow_config],\n"
            "            retry_policy=RetryPolicy(maximum_attempts=3),\n"
            "        )\n"
            "        return db\n",
        )
        _write(
            tmp_path,
            "atlan-sqltest-app/main.py",
            "import asyncio\n"
            "from application_sdk.application import BaseApplication\n"
            "from app.activities import SqlTestActivities\n"
            "from app.workflow import SqlTestWorkflow\n\n"
            "async def main():\n"
            "    app = BaseApplication(name='sqltest')\n"
            "    await app.setup_workflow(\n"
            "        workflow_and_activities_classes=[(SqlTestWorkflow, SqlTestActivities)]\n"
            "    )\n"
            "    await app.start(workflow_class=SqlTestWorkflow)\n\n"
            "if __name__ == '__main__':\n"
            "    asyncio.run(main())\n",
        )
        return root

    def test_decorators_removed(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="sql_metadata")
        activities_src = _read(root, "app/activities.py")
        assert "@activity.defn" not in activities_src
        workflow_src = _read(root, "app/workflow.py")
        assert "@workflow.defn" not in workflow_src
        assert "@workflow.run" not in workflow_src

    def test_task_decorator_added(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="sql_metadata")
        activities_src = _read(root, "app/activities.py")
        assert "@task" in activities_src

    def test_execute_activity_method_rewritten(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="sql_metadata")
        workflow_src = _read(root, "app/workflow.py")
        assert "execute_activity_method" not in workflow_src
        assert "self.fetch_databases" in workflow_src

    def test_activities_plumbing_removed(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="sql_metadata")
        activities_src = _read(root, "app/activities.py")
        workflow_src = _read(root, "app/workflow.py")
        assert "activities_cls" not in activities_src
        assert "activities_cls" not in workflow_src
        assert "get_activities" not in workflow_src
        assert "activities_instance" not in workflow_src

    def test_entry_point_rewritten(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="sql_metadata")
        main_src = _read(root, "main.py")
        assert "run_dev_combined" in main_src
        assert "setup_workflow" not in main_src

    def test_signatures_rewritten(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="sql_metadata")
        activities_src = _read(root, "app/activities.py")
        # workflow_args → input: FetchDatabasesInput
        assert "workflow_args" not in activities_src
        assert "input" in activities_src

    def test_returns_rewritten(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="sql_metadata")
        activities_src = _read(root, "app/activities.py")
        # Return values are rewritten to typed Output constructors
        # (the old import may still appear — that's cleaned up by ruff F401 in Phase 2d)
        assert "FetchDatabasesOutput(" in activities_src
        assert "FetchSchemasOutput(" in activities_src

    def test_result_summary(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        result = run_codemods(root, connector_type="sql_metadata", dry_run=True)
        assert result.connector_type == "sql_metadata"
        assert result.files_changed == 3
        assert not result.errors


# ---------------------------------------------------------------------------
# Fixture 2: handler with *args/**kwargs and load()
# ---------------------------------------------------------------------------


class TestHandlerRoundtrip:
    def _setup(self, tmp_path: Path) -> Path:
        root = tmp_path / "atlan-handler-app"
        _write(
            tmp_path,
            "atlan-handler-app/app/handler.py",
            "from application_sdk.handlers import BaseHandler\n\n"
            "class MyHandler(BaseHandler):\n"
            "    async def load(self, *args, **kwargs):\n"
            "        self._client = await build_client()\n\n"
            "    async def test_auth(self, *args, **kwargs) -> bool:\n"
            "        return True\n\n"
            "    async def preflight_check(self, *args, **kwargs) -> dict:\n"
            "        return {}\n\n"
            "    async def fetch_metadata(self, *args, **kwargs) -> list:\n"
            "        return []\n",
        )
        return root

    def test_load_method_removed(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root)
        src = _read(root, "app/handler.py")
        assert "def load(" not in src

    def test_handler_signatures_rewritten(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root)
        src = _read(root, "app/handler.py")
        assert "*args" not in src
        assert "**kwargs" not in src

    def test_handler_typed_inputs(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root)
        src = _read(root, "app/handler.py")
        assert "AuthInput" in src
        assert "AuthOutput" in src
        assert "PreflightInput" in src
        assert "MetadataInput" in src

    def test_files_changed_reported(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        result = run_codemods(root, dry_run=True)
        assert result.files_changed == 1


# ---------------------------------------------------------------------------
# Fixture 3: sql_query connector
# ---------------------------------------------------------------------------


class TestSqlQueryRoundtrip:
    def _setup(self, tmp_path: Path) -> Path:
        root = tmp_path / "atlan-query-app"
        _write(
            tmp_path,
            "atlan-query-app/app/activities.py",
            "from temporalio import activity\n\n"
            "class QueryActivities:\n"
            "    @activity.defn\n"
            "    async def get_query_batches(self, workflow_args):\n"
            "        return ActivityStatistics(total_record_count=0)\n\n"
            "    @activity.defn\n"
            "    async def fetch_queries(self, workflow_args):\n"
            "        return ActivityStatistics(total_record_count=0)\n",
        )
        return root

    def test_decorators_removed(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="sql_query")
        src = _read(root, "app/activities.py")
        assert "@activity.defn" not in src

    def test_sql_query_signatures_rewritten(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="sql_query")
        src = _read(root, "app/activities.py")
        assert "workflow_args" not in src
        assert "QueryBatchInput" in src
        assert "QueryFetchInput" in src

    def test_sql_query_returns_rewritten(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="sql_query")
        src = _read(root, "app/activities.py")
        assert "ActivityStatistics" not in src
        assert "QueryBatchOutput" in src


# ---------------------------------------------------------------------------
# Fixture 4: custom connector (partial template methods)
# ---------------------------------------------------------------------------


class TestCustomRoundtrip:
    def _setup(self, tmp_path: Path) -> Path:
        root = tmp_path / "atlan-custom-app"
        _write(
            tmp_path,
            "atlan-custom-app/app/activities.py",
            "from temporalio import activity\n\n"
            "class CustomActivities:\n"
            "    @activity.defn\n"
            "    async def fetch_databases(self, workflow_args):\n"
            "        return ActivityStatistics(total_record_count=0)\n\n"
            "    def _custom_helper(self):\n"
            "        pass\n",
        )
        return root

    def test_decorator_removed_on_custom(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="custom")
        src = _read(root, "app/activities.py")
        assert "@activity.defn" not in src

    def test_custom_method_preserved(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        run_codemods(root, connector_type="custom")
        src = _read(root, "app/activities.py")
        assert "_custom_helper" in src

    def test_dry_run_no_writes(self, tmp_path: Path) -> None:
        root = self._setup(tmp_path)
        original = _read(root, "app/activities.py")
        run_codemods(root, dry_run=True)
        assert _read(root, "app/activities.py") == original


# ---------------------------------------------------------------------------
# F1: JSON report output
# ---------------------------------------------------------------------------


class TestJsonReport:
    def test_to_dict_has_required_keys(self, tmp_path: Path) -> None:
        root = tmp_path / "atlan-report-app"
        _write(
            tmp_path,
            "atlan-report-app/app/activities.py",
            "from temporalio import activity\n\n"
            "class MyActivities:\n"
            "    @activity.defn\n"
            "    async def fetch_databases(self, workflow_args):\n"
            "        pass\n",
        )
        result = run_codemods(root, dry_run=True)
        d = result.to_dict()
        assert "connector_type" in d
        assert "files_changed" in d
        assert "changes_by_file" in d
        assert "errors" in d
        assert "todos_inserted" in d
        assert "generated_at" in d

    def test_write_report_creates_file(self, tmp_path: Path) -> None:
        root = tmp_path / "atlan-report-app"
        _write(
            tmp_path,
            "atlan-report-app/app/activities.py",
            "from temporalio import activity\n\n"
            "class MyActivities:\n"
            "    @activity.defn\n"
            "    async def fetch_databases(self, workflow_args):\n"
            "        pass\n",
        )
        report_path = tmp_path / "migration_report.json"
        run_codemods(root, dry_run=True, output_json=report_path)
        assert report_path.exists()

        import json

        data = json.loads(report_path.read_text())
        assert data["connector_type"] is not None
