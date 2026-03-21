"""Tests for A2: RewriteActivityCallsCodemod."""

from __future__ import annotations

from tests.unit.tools.test_codemods.conftest import transform
from tools.migrate_v3.codemods.rewrite_activity_calls import RewriteActivityCallsCodemod


class TestInstanceMethodForm:
    def test_basic_rewrite(self) -> None:
        source = """\
from temporalio import workflow

class MyWorkflow:
    async def run(self, workflow_config):
        result = await workflow.execute_activity_method(
            activities_instance.fetch_databases,
            args=[workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=timeout,
            heartbeat_timeout=heartbeat,
        )
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        assert "execute_activity_method" not in code
        assert "await self.fetch_databases(workflow_args)" in code
        assert any("fetch_databases" in c for c in changes)

    def test_strips_all_temporal_kwargs(self) -> None:
        source = """\
class MyWorkflow:
    async def run(self, config):
        await workflow.execute_activity_method(
            inst.preflight_check,
            args=[workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=timeout,
            heartbeat_timeout=heartbeat,
            summary="Preflight check",
            schedule_to_close_timeout=sched,
            task_queue="my-queue",
        )
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        assert "retry_policy" not in code
        assert "start_to_close_timeout" not in code
        assert "heartbeat_timeout" not in code
        assert "summary=" not in code
        assert "schedule_to_close_timeout" not in code
        assert "task_queue=" not in code
        assert "await self.preflight_check(workflow_args)" in code

    def test_assignment_preserved(self) -> None:
        source = """\
class MyWorkflow:
    async def run(self, config):
        result = await workflow.execute_activity_method(
            inst.get_workflow_args,
            args=[workflow_config],
            retry_policy=policy,
        )
        return result
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        assert "result = await self.get_workflow_args(workflow_config)" in code

    def test_multiple_calls_all_rewritten(self) -> None:
        source = """\
class MyWorkflow:
    async def run(self, config):
        await workflow.execute_activity_method(
            inst.preflight_check, args=[workflow_args], retry_policy=p,
        )
        await workflow.execute_activity_method(
            inst.fetch_databases, args=[workflow_args], retry_policy=p,
        )
        await workflow.execute_activity_method(
            inst.transform_data, args=[workflow_args], retry_policy=p,
        )
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        assert "execute_activity_method" not in code
        assert "await self.preflight_check(workflow_args)" in code
        assert "await self.fetch_databases(workflow_args)" in code
        assert "await self.transform_data(workflow_args)" in code
        assert len(changes) == 3


class TestClassMethodForm:
    def test_class_method_positional_arg(self) -> None:
        """Athena-style: ClassName.method + positional (not args=[]) arg."""
        source = """\
class AthenaWorkflow:
    async def run(self, workflow_config):
        workflow_args = await workflow.execute_activity_method(
            AthenaActivities.get_workflow_args,
            workflow_config,
            retry_policy=retry_policy,
            start_to_close_timeout=timeout,
        )
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        assert "execute_activity_method" not in code
        assert "await self.get_workflow_args(workflow_config)" in code

    def test_class_method_args_kwarg(self) -> None:
        source = """\
class MyWorkflow:
    async def run(self, cfg):
        await workflow.execute_activity_method(
            MyActivities.fetch_tables,
            args=[workflow_args],
            retry_policy=p,
        )
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        assert "await self.fetch_tables(workflow_args)" in code


class TestDynamicDispatch:
    def test_dynamic_dispatch_left_unchanged(self) -> None:
        """getattr-based dispatch is skipped; a SKIPPED change entry is recorded."""
        source = """\
class MyWorkflow:
    async def run(self, config):
        activity_method = getattr(activities_instance, method_name, None)
        result = await workflow.execute_activity_method(
            activity_method,
            args=[workflow_args],
            retry_policy=policy,
        )
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        # Code is untouched
        assert "execute_activity_method" in code
        assert "activity_method," in code
        # Change entry recorded
        assert any("SKIPPED" in c for c in changes)

    def test_name_variable_left_unchanged(self) -> None:
        """A bare Name (variable reference) in first arg is dynamic dispatch."""
        source = """\
class MyWorkflow:
    async def run(self, config):
        fn = some_registry[key]
        await workflow.execute_activity_method(fn, args=[workflow_args])
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        assert "execute_activity_method" in code
        assert any("SKIPPED" in c for c in changes)


class TestNoOp:
    def test_already_v3_style_unchanged(self) -> None:
        source = """\
class MyApp:
    async def run(self, input):
        result = await self.fetch_databases(input)
        return result
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        assert not changes
        assert "await self.fetch_databases(input)" in code

    def test_unrelated_call_unchanged(self) -> None:
        source = """\
class MyApp:
    async def run(self, config):
        result = await some_other_function(config)
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        assert not changes

    def test_no_data_arg_produces_empty_call(self) -> None:
        """When no args= kwarg and no second positional arg, produces self.method()."""
        source = """\
class MyWorkflow:
    async def run(self, config):
        await workflow.execute_activity_method(
            inst.some_method,
            retry_policy=policy,
        )
"""
        code, changes = transform(source, RewriteActivityCallsCodemod)
        assert "await self.some_method()" in code
        assert any("some_method" in c for c in changes)
