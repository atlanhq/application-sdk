"""Tests for A1: RemoveDecoratorsCodemod."""

from __future__ import annotations

from tests.unit.tools.test_codemods.conftest import transform
from tools.migrate_v3.codemods.remove_decorators import RemoveDecoratorsCodemod


class TestRemoveWorkflowDefn:
    def test_removes_workflow_defn_from_class(self) -> None:
        source = """
            import temporalio.workflow as workflow

            @workflow.defn
            class MyWorkflow:
                pass
        """
        code, changes = transform(source, RemoveDecoratorsCodemod)
        assert "@workflow.defn" not in code
        assert "MyWorkflow" in code
        assert any("workflow.defn" in c for c in changes)

    def test_preserves_unrelated_class_decorators(self) -> None:
        source = """
            import temporalio.workflow as workflow

            @workflow.defn
            @some_other_decorator
            class MyWorkflow:
                pass
        """
        code, changes = transform(source, RemoveDecoratorsCodemod)
        assert "@workflow.defn" not in code
        assert "@some_other_decorator" in code


class TestRemoveWorkflowRun:
    def test_removes_workflow_run_no_task_added(self) -> None:
        source = """
            import temporalio.workflow as workflow

            class MyWorkflow:
                @workflow.run
                async def run(self, workflow_args):
                    pass
        """
        code, changes = transform(source, RemoveDecoratorsCodemod)
        assert "@workflow.run" not in code
        # @task must NOT be added for @workflow.run removal
        assert "@task" not in code
        assert any("workflow.run" in c for c in changes)


class TestRemoveActivityDefn:
    def test_removes_activity_defn_adds_task_with_known_timeout(self) -> None:
        source = """
            import temporalio.activity as activity

            class MyActivities:
                @activity.defn
                async def fetch_databases(self, workflow_args):
                    pass
        """
        code, changes = transform(
            source, RemoveDecoratorsCodemod, connector_type="sql_metadata"
        )
        assert "@activity.defn" not in code
        assert "@task(timeout_seconds=1800)" in code
        assert any("task" in c and "1800" in c for c in changes)

    def test_removes_activity_defn_adds_task_default_timeout_unknown_method(
        self,
    ) -> None:
        source = """
            import temporalio.activity as activity

            class MyActivities:
                @activity.defn
                async def do_something_custom(self, args):
                    pass
        """
        code, changes = transform(source, RemoveDecoratorsCodemod)
        assert "@activity.defn" not in code
        assert "@task(timeout_seconds=600)" in code

    def test_activity_defn_with_name_arg(self) -> None:
        source = """
            import temporalio.activity as activity

            class MyActivities:
                @activity.defn(name="custom-name")
                async def fetch_schemas(self, workflow_args):
                    pass
        """
        code, changes = transform(
            source, RemoveDecoratorsCodemod, connector_type="sql_metadata"
        )
        assert "@activity.defn" not in code
        assert "@task(timeout_seconds=1800)" in code

    def test_task_import_added_to_imports_to_add(self) -> None:
        import libcst as cst

        from tools.migrate_v3.codemods.remove_decorators import (
            RemoveDecoratorsCodemod as C,
        )

        source = (
            "import temporalio.activity as activity\n\n"
            "class MyActivities:\n"
            "    @activity.defn\n"
            "    async def fetch_tables(self, args):\n"
            "        pass\n"
        )
        tree = cst.parse_module(source)
        codemod = C(connector_type="sql_metadata")
        codemod.transform(tree)
        assert ("application_sdk.app", "task") in codemod.imports_to_add


class TestRemoveAutoHeartbeater:
    def test_removes_auto_heartbeater(self) -> None:
        source = """
            class MyActivities:
                @auto_heartbeater
                async def fetch_columns(self, args):
                    pass
        """
        code, changes = transform(source, RemoveDecoratorsCodemod)
        assert "@auto_heartbeater" not in code
        assert any("auto_heartbeater" in c for c in changes)

    def test_activity_defn_and_auto_heartbeater_to_single_task(self) -> None:
        source = """
            import temporalio.activity as activity

            class MyActivities:
                @activity.defn
                @auto_heartbeater
                async def fetch_tables(self, args):
                    pass
        """
        code, changes = transform(
            source, RemoveDecoratorsCodemod, connector_type="sql_metadata"
        )
        assert "@activity.defn" not in code
        assert "@auto_heartbeater" not in code
        assert "@task(timeout_seconds=1800)" in code
        # Only one @task decorator should appear
        assert code.count("@task") == 1


class TestNoOp:
    def test_clean_code_unchanged(self) -> None:
        source = """
            from application_sdk.app import task

            class MyApp:
                @task(timeout_seconds=1800)
                async def fetch_databases(self, input):
                    pass
        """
        code, changes = transform(source, RemoveDecoratorsCodemod)
        assert not changes

    def test_preserves_staticmethod_and_override(self) -> None:
        source = """
            import temporalio.activity as activity

            class MyActivities:
                @activity.defn
                @staticmethod
                async def fetch_procedures(args):
                    pass
        """
        code, changes = transform(source, RemoveDecoratorsCodemod)
        assert "@staticmethod" in code
        assert "@activity.defn" not in code
