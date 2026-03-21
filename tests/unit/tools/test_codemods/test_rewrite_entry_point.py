"""Tests for A7: RewriteEntryPointCodemod."""

from __future__ import annotations

from pathlib import Path

from tests.unit.tools.test_codemods.conftest import transform
from tools.migrate_v3.codemods.rewrite_entry_point import (
    RewriteEntryPointCodemod,
    derive_app_class_name,
)


class TestDeriveAppClassName:
    def test_standard_convention(self) -> None:
        assert derive_app_class_name(Path("atlan-anaplan-app")) == "AnaplanApp"

    def test_hyphenated_name(self) -> None:
        assert (
            derive_app_class_name(Path("atlan-schema-registry-app"))
            == "SchemaRegistryApp"
        )

    def test_no_prefix_suffix(self) -> None:
        assert derive_app_class_name(Path("my-connector")) == "MyConnectorApp"

    def test_fallback_empty(self) -> None:
        # If after stripping we have nothing, fallback to MyApp
        assert derive_app_class_name(Path("atlan--app")) == "MyApp"

    def test_no_atlan_prefix(self) -> None:
        assert derive_app_class_name(Path("athena-app")) == "AthenaApp"


class TestStandardRewrite:
    def test_simple_no_handler(self) -> None:
        source = """\
import asyncio
from application_sdk.application import BaseApplication
from app.workflows import PublishWorkflow
from app.activities import PublishActivities

async def main():
    app = BaseApplication(name=APPLICATION_NAME)
    await app.setup_workflow(
        workflow_and_activities_classes=[(PublishWorkflow, PublishActivities)]
    )
    await app.start(workflow_class=PublishWorkflow)

if __name__ == "__main__":
    asyncio.run(main())
"""
        code, changes = transform(
            source, RewriteEntryPointCodemod, app_class_name="PublishApp"
        )
        # The function body is rewritten (import removal is a pipeline concern)
        assert "setup_workflow" not in code
        assert "await run_dev_combined(PublishApp)" in code
        assert "handler_class" not in code
        assert any("run_dev_combined" in c for c in changes)

    def test_with_handler_class(self) -> None:
        source = """\
import asyncio
from application_sdk.application import BaseApplication

async def main():
    application = BaseApplication(
        name=APPLICATION_NAME,
        client_class=AnaplanApiClient,
        handler_class=AnaplanHandler,
    )
    await application.setup_workflow(
        workflow_and_activities_classes=[
            (AnaplanMetadataExtractionWorkflow, AnaplanMetadataExtractionActivities)
        ]
    )
    await application.start(workflow_class=AnaplanMetadataExtractionWorkflow, has_configmap=True)

if __name__ == "__main__":
    asyncio.run(main())
"""
        code, changes = transform(
            source, RewriteEntryPointCodemod, app_class_name="AnaplanApp"
        )
        assert "setup_workflow" not in code
        assert (
            "await run_dev_combined(AnaplanApp, handler_class=AnaplanHandler)" in code
        )

    def test_has_configmap_stripped(self) -> None:
        """has_configmap=True in start() is stripped — whole body is replaced."""
        source = """\
async def main():
    app = BaseApplication(name=NAME)
    await app.setup_workflow(workflow_and_activities_classes=[(WF, ACT)])
    await app.start(workflow_class=WF, has_configmap=True)
"""
        code, changes = transform(
            source, RewriteEntryPointCodemod, app_class_name="MyApp"
        )
        assert "has_configmap" not in code
        assert "run_dev_combined" in code

    def test_ui_enabled_stripped(self) -> None:
        source = """\
async def main():
    application = BaseApplication(name=APP_NAME, handler_class=MyHandler)
    await application.setup_workflow(workflow_and_activities_classes=[(WF, ACT)])
    await application.start(workflow_class=WF, ui_enabled=True, has_configmap=True)
"""
        code, changes = transform(
            source, RewriteEntryPointCodemod, app_class_name="SchemaRegistryApp"
        )
        assert "ui_enabled" not in code
        assert "run_dev_combined(SchemaRegistryApp, handler_class=MyHandler)" in code

    def test_run_dev_combined_import_added(self) -> None:
        source = """\
async def main():
    app = BaseApplication(name=NAME)
    await app.setup_workflow(workflow_and_activities_classes=[(WF, ACT)])
    await app.start(workflow_class=WF)
"""
        codemod = RewriteEntryPointCodemod(app_class_name="MyApp")
        import libcst as cst

        tree = cst.parse_module(source)
        codemod.transform(tree)
        assert ("application_sdk.main", "run_dev_combined") in codemod.imports_to_add


class TestComplexEntryPoint:
    def test_start_worker_gets_todo(self) -> None:
        """Athena-style split worker/server — too complex to auto-rewrite."""
        source = """\
async def main():
    application = BaseApplication(name=APPLICATION_NAME)
    await application.setup_workflow(workflow_and_activities_classes=[(WF, ACT)])
    has_worker = APPLICATION_MODE in (ApplicationMode.LOCAL, ApplicationMode.WORKER)
    if has_worker:
        await application.start_worker(daemon=True)
"""
        code, changes = transform(
            source, RewriteEntryPointCodemod, app_class_name="AthenaApp"
        )
        # Code body is unchanged
        assert "BaseApplication" in code
        assert "start_worker" in code
        # But a TODO comment is prepended
        assert "TODO(migrate-v3)" in code
        assert any("Skipped" in c for c in changes)

    def test_include_router_gets_todo(self) -> None:
        source = """\
async def main():
    application = BaseApplication(name=APPLICATION_NAME)
    await application.setup_workflow(workflow_and_activities_classes=[(WF, ACT)])
    await application.setup_server(workflow_class=WF)
    application.server.app.include_router(manifest_router)
    await application.start_server()
"""
        code, changes = transform(
            source, RewriteEntryPointCodemod, app_class_name="AthenaApp"
        )
        assert "include_router" in code
        assert "TODO(migrate-v3)" in code
        assert any("Skipped" in c for c in changes)

    def test_no_base_application_unchanged(self) -> None:
        source = """\
async def main():
    await run_dev_combined(MyApp, handler_class=MyHandler)
"""
        code, changes = transform(
            source, RewriteEntryPointCodemod, app_class_name="MyApp"
        )
        assert not changes
        assert "run_dev_combined" in code


class TestNonMainFunction:
    def test_non_main_function_unchanged(self) -> None:
        source = """\
async def setup():
    application = BaseApplication(name=APP_NAME)
    await application.setup_workflow(workflow_and_activities_classes=[(WF, ACT)])
    await application.start(workflow_class=WF)
"""
        code, changes = transform(
            source, RewriteEntryPointCodemod, app_class_name="MyApp"
        )
        # Only 'main()' is rewritten — other functions are not touched.
        assert not changes
        assert "BaseApplication" in code
