"""Tests for A8: run_codemods pipeline."""

from __future__ import annotations

from pathlib import Path

from tools.migrate_v3.run_codemods import RunResult, derive_app_class_name, run_codemods


def _write(tmp_path: Path, filename: str, source: str) -> Path:
    p = tmp_path / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# derive_app_class_name
# ---------------------------------------------------------------------------


class TestDeriveAppClassName:
    def test_atlan_prefix_and_app_suffix(self) -> None:
        assert derive_app_class_name(Path("atlan-anaplan-app")) == "AnaplanApp"

    def test_hyphenated_middle(self) -> None:
        assert (
            derive_app_class_name(Path("atlan-schema-registry-app"))
            == "SchemaRegistryApp"
        )

    def test_fallback_no_convention(self) -> None:
        # No atlan- prefix, no -app suffix → still produces a reasonable name
        result = derive_app_class_name(Path("my-connector"))
        assert result == "MyConnectorApp"


# ---------------------------------------------------------------------------
# RunResult.todos_inserted
# ---------------------------------------------------------------------------


class TestRunResultTodosInserted:
    def test_counts_skipped_entries(self) -> None:
        result = RunResult(connector_type="custom", confidence=0.5)
        result.changes_by_file["workflow.py"] = {
            "A2": [
                "SKIPPED: dynamic dispatch in execute_activity_method (rewrite manually)"
            ]
        }
        assert result.todos_inserted == 1

    def test_zero_when_no_skipped(self) -> None:
        result = RunResult(connector_type="sql_metadata", confidence=1.0)
        result.changes_by_file["activities.py"] = {
            "A1": ["Removed @activity.defn from fetch_databases"]
        }
        assert result.todos_inserted == 0


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_a1_a2_a6_applied(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "atlan-test-app/app/workflow.py",
            "from temporalio import workflow\n"
            "from temporalio.common import RetryPolicy\n\n"
            "@workflow.defn\n"
            "class TestWorkflow:\n"
            "    activities_cls = TestActivities\n\n"
            "    @staticmethod\n"
            "    def get_activities(activities):\n"
            "        return [activities.fetch_databases]\n\n"
            "    @workflow.run\n"
            "    async def run(self, workflow_config):\n"
            "        activities_instance = self.activities_cls()\n"
            "        result = await workflow.execute_activity_method(\n"
            "            activities_instance.fetch_databases,\n"
            "            args=[workflow_config],\n"
            "            retry_policy=RetryPolicy(maximum_attempts=3),\n"
            "        )\n"
            "        return result\n",
        )
        root = tmp_path / "atlan-test-app"
        result = run_codemods(root, connector_type="sql_metadata", dry_run=True)

        assert result.connector_type == "sql_metadata"
        assert result.files_changed == 1
        file_changes = result.changes_by_file["app/workflow.py"]
        # A1 removed decorators
        assert "A1" in file_changes
        # A2 rewrote execute_activity_method
        assert "A2" in file_changes
        # A6 removed activities plumbing
        assert "A6" in file_changes

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            "atlan-test-app/app/activities.py",
            "from temporalio import activity\n\n"
            "class MyActivities:\n"
            "    @activity.defn\n"
            "    async def fetch_databases(self, workflow_args):\n"
            "        pass\n",
        )
        original = p.read_text()
        root = tmp_path / "atlan-test-app"
        result = run_codemods(root, dry_run=True)
        assert p.read_text() == original  # File unchanged
        assert result.files_changed == 1  # But changes detected

    def test_writes_files_when_not_dry_run(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            "atlan-test-app/app/activities.py",
            "from temporalio import activity\n\n"
            "class MyActivities:\n"
            "    @activity.defn\n"
            "    async def fetch_databases(self, workflow_args):\n"
            "        pass\n",
        )
        original = p.read_text()
        root = tmp_path / "atlan-test-app"
        run_codemods(root, dry_run=False)
        assert p.read_text() != original  # File was modified

    def test_skip_a7_skips_entry_point(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "atlan-test-app/main.py",
            "from application_sdk.application import BaseApplication\n\n"
            "async def main():\n"
            "    app = BaseApplication(name='test')\n"
            "    await app.setup_workflow(workflow_and_activities_classes=[(WF, ACT)])\n"
            "    await app.start(workflow_class=WF)\n",
        )
        root = tmp_path / "atlan-test-app"
        result = run_codemods(root, skip=["A7"])
        # With A7 skipped, main.py should not have been changed
        main_changes = result.changes_by_file.get("main.py", {})
        assert "A7" not in main_changes

    def test_parse_error_recorded_in_errors(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "atlan-test-app/app/broken.py",
            "def this is not valid python!!!\n",
        )
        root = tmp_path / "atlan-test-app"
        result = run_codemods(root)
        assert len(result.errors) == 1
        assert "broken.py" in result.errors[0]

    def test_connector_type_override(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "atlan-test-app/app/activities.py",
            "from temporalio import activity\n\n"
            "class MyActivities:\n"
            "    @activity.defn\n"
            "    async def fetch_databases(self, workflow_args):\n"
            "        return ActivityStatistics(total_record_count=0)\n",
        )
        root = tmp_path / "atlan-test-app"
        result = run_codemods(root, connector_type="sql_metadata")
        assert result.connector_type == "sql_metadata"
