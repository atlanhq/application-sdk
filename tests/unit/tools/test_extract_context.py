"""Tests for B1 + F4: extract_context pipeline."""

from __future__ import annotations

import textwrap
from pathlib import Path

from tools.migrate_v3.extract_context import (
    ConnectorContext,
    _ClassExtractor,
    _compute_difficulty,
    _scan_infra,
    context_summary,
    extract_context,
)


def _write(tmp_path: Path, filename: str, source: str) -> Path:
    p = tmp_path / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(source), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _ClassExtractor
# ---------------------------------------------------------------------------


class TestClassExtractor:
    def _extract(self, source: str) -> list:
        import libcst as cst

        extractor = _ClassExtractor()
        cst.parse_module(textwrap.dedent(source)).visit(extractor)
        return extractor.classes

    def test_activities_role(self) -> None:
        classes = self._extract(
            """\
            class MyActivities(BaseSQLMetadataExtractionActivities):
                pass
            """
        )
        assert len(classes) == 1
        assert classes[0].role == "activities"

    def test_workflow_role(self) -> None:
        classes = self._extract(
            """\
            class MyWorkflow(BaseSQLMetadataExtractionWorkflow):
                pass
            """
        )
        assert classes[0].role == "workflow"

    def test_handler_role(self) -> None:
        classes = self._extract(
            """\
            class MyHandler(BaseHandler):
                pass
            """
        )
        assert classes[0].role == "handler"

    def test_app_role_v3(self) -> None:
        classes = self._extract(
            """\
            class MyApp(SqlMetadataExtractor):
                pass
            """
        )
        assert classes[0].role == "app"

    def test_unknown_role(self) -> None:
        classes = self._extract(
            """\
            class Util:
                pass
            """
        )
        assert classes[0].role == "unknown"

    def test_template_method_classified(self) -> None:
        classes = self._extract(
            """\
            class MyActivities(BaseSQLMetadataExtractionActivities):
                async def fetch_databases(self, workflow_args):
                    pass
            """
        )
        cls = classes[0]
        assert len(cls.template_methods) == 1
        assert cls.template_methods[0].name == "fetch_databases"
        assert cls.template_methods[0].is_template is True
        assert len(cls.custom_methods) == 0

    def test_custom_method_classified(self) -> None:
        classes = self._extract(
            """\
            class MyActivities(BaseSQLMetadataExtractionActivities):
                def _build_query(self):
                    pass
            """
        )
        cls = classes[0]
        assert len(cls.custom_methods) == 1
        assert cls.custom_methods[0].name == "_build_query"

    def test_load_method_tracked(self) -> None:
        classes = self._extract(
            """\
            class MyHandler(BaseHandler):
                async def load(self, *args, **kwargs):
                    pass
            """
        )
        assert classes[0].has_load_method is True
        assert len(classes[0].custom_methods) == 0

    def test_args_kwargs_detected(self) -> None:
        classes = self._extract(
            """\
            class MyHandler(BaseHandler):
                async def test_auth(self, *args, **kwargs):
                    pass
            """
        )
        method = classes[0].template_methods[0]
        assert method.has_args_kwargs is True

    def test_typed_method_no_args_kwargs(self) -> None:
        classes = self._extract(
            """\
            class MyHandler(BaseHandler):
                async def test_auth(self, input):
                    pass
            """
        )
        method = classes[0].template_methods[0]
        assert method.has_args_kwargs is False

    def test_nested_function_not_classified_as_method(self) -> None:
        """Nested functions inside a method should NOT appear as class methods."""
        classes = self._extract(
            """\
            class MyActivities(BaseSQLMetadataExtractionActivities):
                async def fetch_databases(self, workflow_args):
                    def inner_helper():
                        pass
            """
        )
        cls = classes[0]
        assert len(cls.template_methods) == 1
        # inner_helper must not appear as a separate method
        assert all(m.name != "inner_helper" for m in cls.custom_methods)

    def test_multiple_classes(self) -> None:
        classes = self._extract(
            """\
            class MyWorkflow(BaseSQLMetadataExtractionWorkflow):
                pass

            class MyActivities(BaseSQLMetadataExtractionActivities):
                async def fetch_databases(self, workflow_args):
                    pass
            """
        )
        assert len(classes) == 2
        roles = {c.role for c in classes}
        assert roles == {"workflow", "activities"}

    def test_bases_extracted(self) -> None:
        classes = self._extract(
            """\
            class MyActivities(module.BaseSQLMetadataExtractionActivities):
                pass
            """
        )
        assert classes[0].bases == ["BaseSQLMetadataExtractionActivities"]


# ---------------------------------------------------------------------------
# _scan_infra
# ---------------------------------------------------------------------------


class TestScanInfra:
    def test_secret_store_detected(self) -> None:
        usages = _scan_infra("self._secret_store.get_credentials()", "file.py")
        kinds = {u.kind for u in usages}
        assert "secret_store" in kinds

    def test_state_store_detected(self) -> None:
        usages = _scan_infra("await self._state_store.set(key, value)", "file.py")
        kinds = {u.kind for u in usages}
        assert "state_store" in kinds

    def test_dapr_client_detected(self) -> None:
        usages = _scan_infra("client = DaprClient()", "file.py")
        kinds = {u.kind for u in usages}
        assert "dapr" in kinds

    def test_no_false_positives_on_clean_code(self) -> None:
        usages = _scan_infra(
            "async def fetch_databases(self, input):\n    pass\n", "f.py"
        )
        assert usages == []

    def test_count_reflects_occurrences(self) -> None:
        source = "self._secret_store.get()\nself._secret_store.get()\n"
        usages = _scan_infra(source, "f.py")
        secret = next(u for u in usages if u.kind == "secret_store")
        assert secret.count == 2


# ---------------------------------------------------------------------------
# _compute_difficulty
# ---------------------------------------------------------------------------


class TestComputeDifficulty:
    def _make_ctx(
        self,
        *,
        custom_per_class=0,
        n_classes=1,
        infra_kinds=None,
        complex_entry=False,
        temporal=0,
    ):
        from tools.migrate_v3.extract_context import ClassInfo, InfraUsage, MethodInfo

        classes = []
        for i in range(n_classes):
            cls = ClassInfo(name=f"Cls{i}", bases=[], role="activities")
            for j in range(custom_per_class):
                cls.custom_methods.append(
                    MethodInfo(
                        name=f"m{j}",
                        is_template=False,
                        has_args_kwargs=False,
                        body_loc=1,
                    )
                )
            classes.append(cls)

        infra = [InfraUsage(kind=k, file="f.py", count=1) for k in (infra_kinds or [])]
        return classes, infra, complex_entry, temporal

    def test_simple_no_extras(self) -> None:
        label, score = _compute_difficulty(*self._make_ctx())
        assert label == "simple"
        assert score < 0.3

    def test_custom_methods_raise_score(self) -> None:
        _, score_base = _compute_difficulty(*self._make_ctx(custom_per_class=0))
        _, score_custom = _compute_difficulty(*self._make_ctx(custom_per_class=2))
        assert score_custom > score_base

    def test_complex_entry_raises_score(self) -> None:
        _, score_simple = _compute_difficulty(*self._make_ctx())
        _, score_complex = _compute_difficulty(*self._make_ctx(complex_entry=True))
        assert score_complex >= score_simple + 0.2

    def test_many_infra_types_raises_score(self) -> None:
        _, score = _compute_difficulty(
            *self._make_ctx(infra_kinds=["secret_store", "state_store", "object_store"])
        )
        assert score >= 0.3  # at least moderate

    def test_temporal_patterns_raise_score(self) -> None:
        _, s0 = _compute_difficulty(*self._make_ctx(temporal=0))
        _, s1 = _compute_difficulty(*self._make_ctx(temporal=3))
        assert s1 > s0

    def test_score_capped_at_one(self) -> None:
        # Max out everything
        _, score = _compute_difficulty(
            *self._make_ctx(
                custom_per_class=10,
                n_classes=5,
                infra_kinds=[
                    "secret_store",
                    "state_store",
                    "object_store",
                    "event_store",
                ],
                complex_entry=True,
                temporal=10,
            )
        )
        assert score == 1.0

    def test_complex_label(self) -> None:
        label, _ = _compute_difficulty(
            *self._make_ctx(
                custom_per_class=3,
                infra_kinds=["secret_store", "state_store", "object_store"],
                complex_entry=True,
                temporal=5,
            )
        )
        assert label == "complex"


# ---------------------------------------------------------------------------
# extract_context end-to-end
# ---------------------------------------------------------------------------


class TestExtractContext:
    def test_sql_metadata_connector(self, tmp_path: Path) -> None:
        root = tmp_path / "atlan-test-app"
        _write(
            tmp_path,
            "atlan-test-app/app/activities.py",
            """\
            from temporalio import activity
            from application_sdk.activities import BaseSQLMetadataExtractionActivities

            class TestActivities(BaseSQLMetadataExtractionActivities):
                @activity.defn
                async def fetch_databases(self, workflow_args):
                    return ActivityStatistics(total_record_count=0)

                def _build_query(self):
                    pass
            """,
        )
        ctx = extract_context(root)
        assert ctx.connector_type == "sql_metadata"
        assert len(ctx.classes) == 1
        cls = ctx.classes[0]
        assert cls.role == "activities"
        assert any(m.name == "fetch_databases" for m in cls.template_methods)
        assert any(m.name == "_build_query" for m in cls.custom_methods)

    def test_test_files_excluded(self, tmp_path: Path) -> None:
        root = tmp_path / "atlan-test-app"
        _write(
            tmp_path,
            "atlan-test-app/tests/test_activities.py",
            """\
            class TestActivities(BaseSQLMetadataExtractionActivities):
                pass
            """,
        )
        _write(
            tmp_path,
            "atlan-test-app/app/activities.py",
            """\
            class MyActivities(BaseSQLMetadataExtractionActivities):
                pass
            """,
        )
        ctx = extract_context(root)
        names = [c.name for c in ctx.classes]
        assert "TestActivities" not in names
        assert "MyActivities" in names

    def test_infra_usage_detected(self, tmp_path: Path) -> None:
        root = tmp_path / "atlan-test-app"
        _write(
            tmp_path,
            "atlan-test-app/app/activities.py",
            """\
            class MyActivities:
                async def fetch_databases(self, workflow_args):
                    creds = await self._secret_store.get_credentials()
                    return creds
            """,
        )
        ctx = extract_context(root)
        kinds = {u.kind for u in ctx.infra_usages}
        assert "secret_store" in kinds

    def test_parse_error_skips_file(self, tmp_path: Path) -> None:
        root = tmp_path / "atlan-test-app"
        _write(tmp_path, "atlan-test-app/app/broken.py", "def this is broken!!!\n")
        _write(
            tmp_path,
            "atlan-test-app/app/good.py",
            "class MyActivities(BaseSQLMetadataExtractionActivities): pass\n",
        )
        ctx = extract_context(root)
        # Good file still processed
        assert any(c.name == "MyActivities" for c in ctx.classes)

    def test_complex_entry_point_detected(self, tmp_path: Path) -> None:
        root = tmp_path / "atlan-test-app"
        _write(
            tmp_path,
            "atlan-test-app/main.py",
            """\
            async def main():
                app = BaseApplication(name="x")
                await app.start_worker(daemon=True)
            """,
        )
        ctx = extract_context(root)
        assert ctx.has_complex_entry_point is True

    def test_simple_entry_point_not_flagged(self, tmp_path: Path) -> None:
        root = tmp_path / "atlan-test-app"
        _write(
            tmp_path,
            "atlan-test-app/main.py",
            """\
            async def main():
                app = BaseApplication(name="x")
                await app.setup_workflow(workflow_and_activities_classes=[(WF, ACT)])
                await app.start(workflow_class=WF)
            """,
        )
        ctx = extract_context(root)
        assert ctx.has_complex_entry_point is False

    def test_loc_counted(self, tmp_path: Path) -> None:
        root = tmp_path / "atlan-test-app"
        _write(
            tmp_path,
            "atlan-test-app/app/activities.py",
            """\
            # This is a comment
            class MyActivities:
                async def fetch_databases(self, workflow_args):
                    pass
            """,
        )
        ctx = extract_context(root)
        assert ctx.total_loc > 0


# ---------------------------------------------------------------------------
# context_summary
# ---------------------------------------------------------------------------


class TestContextSummary:
    def _make_ctx(self) -> ConnectorContext:
        from tools.migrate_v3.extract_context import ClassInfo, InfraUsage, MethodInfo

        cls = ClassInfo(
            name="MyActivities",
            bases=["BaseSQLMetadataExtractionActivities"],
            role="activities",
            template_methods=[
                MethodInfo("fetch_databases", True, False, 5),
                MethodInfo("fetch_schemas", True, True, 3),
            ],
            custom_methods=[MethodInfo("_build_query", False, False, 8)],
        )
        return ConnectorContext(
            connector_type="sql_metadata",
            confidence=1.0,
            already_migrated=False,
            evidence=[],
            classes=[cls],
            infra_usages=[InfraUsage("secret_store", "activities.py", 2)],
            has_complex_entry_point=False,
            temporal_v2_count=0,
            total_loc=100,
            difficulty="moderate",
            difficulty_score=0.35,
        )

    def test_connector_type_in_summary(self) -> None:
        ctx = self._make_ctx()
        summary = context_summary(ctx)
        assert "sql_metadata" in summary

    def test_class_name_in_summary(self) -> None:
        ctx = self._make_ctx()
        summary = context_summary(ctx)
        assert "MyActivities" in summary

    def test_template_methods_listed(self) -> None:
        ctx = self._make_ctx()
        summary = context_summary(ctx)
        assert "fetch_databases" in summary

    def test_args_kwargs_warning_shown(self) -> None:
        ctx = self._make_ctx()
        summary = context_summary(ctx)
        # fetch_schemas has has_args_kwargs=True
        assert "fetch_schemas" in summary
        assert "WARNING" in summary

    def test_infra_usage_listed(self) -> None:
        ctx = self._make_ctx()
        summary = context_summary(ctx)
        assert "secret_store" in summary

    def test_already_migrated_note(self) -> None:
        ctx = self._make_ctx()
        ctx.already_migrated = True
        summary = context_summary(ctx)
        assert "Already migrated" in summary

    def test_difficulty_shown(self) -> None:
        ctx = self._make_ctx()
        summary = context_summary(ctx)
        assert "moderate" in summary

    def test_to_dict_serialisable(self) -> None:
        import json

        ctx = self._make_ctx()
        d = ctx.to_dict()
        # Must be JSON-serialisable
        json.dumps(d)
        assert d["connector_type"] == "sql_metadata"
        assert isinstance(d["classes"], list)
