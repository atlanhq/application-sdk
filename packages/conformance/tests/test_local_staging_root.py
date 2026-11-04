"""Meta-tests for P063 LocalStagingOutsideTemporaryPath.

P063 flags a ``WORKFLOW_OUTPUT_PATH_TEMPLATE.format(...)`` call that is not
rooted under ``TEMPORARY_PATH``: the template is an object-store key, and used as
a local directory it lands on the container working directory, which is read-only
on many customer-hosted runtimes.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from conformance.suite.checks.local_staging_root import (
    RULE_ID,
    SERIES,
    discover,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding


def _rule(src: str, file: str = "app/connector.py") -> list[Finding]:
    return [f for f in scan_text(dedent(src), file) if f.rule_id == RULE_ID]


def test_series_and_catalog_entry() -> None:
    assert SERIES == "P"
    assert RULE_ID == "P063"
    rule = get_rule(RULE_ID)
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert not rule.autofixable


# ── Fires: the unrooted-key shape ────────────────────────────────────────────


def test_fires_on_helper_returning_the_bare_template() -> None:
    fs = _rule(
        """\
        from application_sdk.constants import APPLICATION_NAME, WORKFLOW_OUTPUT_PATH_TEMPLATE

        def _resolve_output_path(provided):
            if provided:
                return provided
            info = workflow.info()
            return WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                application_name=APPLICATION_NAME,
                workflow_id=info.workflow_id,
                run_id=info.run_id,
            )

        async def run(input):
            out = _resolve_output_path(input.output_path)
            writer = ParquetFileWriter(path=out)
        """
    )
    assert [f.line for f in fs] == [7]
    assert not fs[0].suppressed
    assert "TEMPORARY_PATH" in fs[0].message


def test_fires_on_template_bound_to_an_unrooted_local() -> None:
    fs = _rule(
        """\
        def run(info):
            base = WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                application_name="a", workflow_id=info.workflow_id, run_id=info.run_id
            )
            os.makedirs(base, exist_ok=True)
        """
    )
    assert [f.line for f in fs] == [2]


def test_fires_through_constants_attribute_and_import_alias() -> None:
    attr = _rule(
        """\
        from application_sdk import constants

        def f(info):
            return constants.WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                application_name="a", workflow_id="w", run_id="r"
            )
        """
    )
    alias = _rule(
        """\
        from application_sdk.constants import WORKFLOW_OUTPUT_PATH_TEMPLATE as TPL

        def f(info):
            return TPL.format(application_name="a", workflow_id="w", run_id="r")
        """
    )
    assert [f.line for f in attr] == [4]
    assert [f.line for f in alias] == [4]


def test_fires_when_helper_has_one_unrooted_caller() -> None:
    fs = _rule(
        """\
        def _key(info):
            return WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                application_name="a", workflow_id=info.workflow_id, run_id=info.run_id
            )

        def rooted(info):
            return os.path.join(TEMPORARY_PATH, _key(info))

        def bare(info):
            return _key(info)
        """
    )
    assert [f.line for f in fs] == [2]


# ── Does not fire: the Oracle / MSSQL / SDK shapes ───────────────────────────


def test_silent_on_oracle_shape_rooted_helper_without_the_template() -> None:
    assert not _rule(
        """\
        import os
        from application_sdk.constants import APPLICATION_NAME, TEMPORARY_PATH

        def _local_run_root(workflow_id, run_id, output_path=""):
            key = normalize_key(output_path) or (
                f"artifacts/apps/{APPLICATION_NAME}/workflows/{workflow_id}/{run_id}"
            )
            return os.path.join(TEMPORARY_PATH, key)
        """
    )


def test_silent_when_the_join_roots_the_template_inline() -> None:
    assert not _rule(
        """\
        def base(info):
            if info.output_path:
                return info.output_path
            return os.path.join(
                TEMPORARY_PATH,
                WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                    application_name=APPLICATION_NAME,
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                ),
            )
        """
    )


def test_silent_when_a_local_holding_the_template_is_rooted_later() -> None:
    assert not _rule(
        """\
        def base(info):
            key = WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                application_name="a", workflow_id=info.workflow_id, run_id=info.run_id
            )
            return os.path.join(TEMPORARY_PATH, key)
        """
    )


def test_silent_when_every_caller_of_a_returning_helper_roots_it() -> None:
    assert not _rule(
        """\
        def _key(info):
            return WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                application_name="a", workflow_id=info.workflow_id, run_id=info.run_id
            )

        def base(info):
            return os.path.join(TEMPORARY_PATH, _key(info))
        """
    )


def test_silent_when_the_value_is_only_an_object_store_key() -> None:
    assert not _rule(
        """\
        def prefix(info):
            return get_object_store_prefix(
                WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                    application_name="a", workflow_id=info.workflow_id, run_id=info.run_id
                )
            )

        def ref(info):
            return FileReference(
                storage_path=WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                    application_name="a", workflow_id=info.workflow_id, run_id=info.run_id
                )
            )
        """
    )


def test_silent_on_literal_artifact_keys_and_other_format_calls() -> None:
    assert not _rule(
        """\
        def keys(app, guid, qn):
            a = f"persistent-artifacts/apps/{app}/credentials/{guid}/config.json"
            b = f"argo-artifacts/{qn}/lineage/current-state"
            c = "artifacts/apps/{app}/x".format(app=app)
            return a, b, c
        """
    )


def test_discovery_skips_test_code(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "connector.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "conftest.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app" / "test_connector.py").write_text("x = 1\n", encoding="utf-8")
    assert [p.name for p in discover(tmp_path)] == ["connector.py"]


# ── Suppression ──────────────────────────────────────────────────────────────

_UNROOTED = """\
def key(info):
    {above}return WORKFLOW_OUTPUT_PATH_TEMPLATE.format(  {inline}
        application_name="a", workflow_id=info.workflow_id, run_id=info.run_id
    )
"""


def test_suppressed_inline() -> None:
    src = _UNROOTED.format(
        above="", inline="# conformance: ignore[P063] only ever an object-store key"
    )
    fs = _rule(src)
    assert len(fs) == 1
    assert fs[0].suppressed
    assert fs[0].suppression_justification == "only ever an object-store key"


def test_suppressed_by_comment_line_above() -> None:
    src = _UNROOTED.format(
        above="# conformance: ignore[P063] only ever an object-store key\n    ",
        inline="",
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].suppressed


def test_other_rule_directive_does_not_suppress() -> None:
    fs = _rule(_UNROOTED.format(above="", inline="# conformance: ignore[P060] nope"))
    assert len(fs) == 1 and not fs[0].suppressed
