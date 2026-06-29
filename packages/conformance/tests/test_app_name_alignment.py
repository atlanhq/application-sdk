"""Tests for P025 AppNameContractCodeDrift check.

Covers the alignment invariant between the App-family leaf class's derived name
(code, authoritative) and the committed contract artifacts (``atlan.yaml`` /
``manifest.json``) and env var (``.env.example``).

Each test builds a minimal temporary repo tree in ``tmp_path``, calls
:func:`scan_all`, and asserts on the resulting findings.

Naming conventions
------------------
* ``py_files``: ``{relative_path: source_text}`` written under ``tmp_path``.
  Source text uses bare names (``Input``, ``Output``) because the AST scanner
  never executes the code; unresolved references are irrelevant.
* ``atlan_yaml``: ``None`` (absent) or the YAML text to write to
  ``{tmp_path}/atlan.yaml``.
* ``env_example``: ``None`` (absent) or the env text to write to
  ``{tmp_path}/.env.example``.
* ``manifest_json``: ``None`` (absent) or a dict written to
  ``{tmp_path}/app/generated/manifest.json`` (single-EP fallback).
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest
from conformance.suite.checks.app_name_alignment import scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_repo(
    tmp_path: Path,
    *,
    py_files: dict[str, str],
    atlan_yaml: str | None = None,
    env_example: str | None = None,
    manifest_json: dict | None = None,
) -> list[Path]:
    """Write repo files under *tmp_path* and return the list of Python paths."""
    paths: list[Path] = []
    for name, src in py_files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
        paths.append(p)
    if atlan_yaml is not None:
        (tmp_path / "atlan.yaml").write_text(atlan_yaml)
    if env_example is not None:
        (tmp_path / ".env.example").write_text(env_example)
    if manifest_json is not None:
        gen = tmp_path / "app" / "generated"
        gen.mkdir(parents=True, exist_ok=True)
        (gen / "manifest.json").write_text(json.dumps(manifest_json))
    return paths


def _run(
    tmp_path: Path,
    py_files: dict[str, str],
    *,
    atlan_yaml: str | None = None,
    env_example: str | None = None,
    manifest_json: dict | None = None,
) -> list:
    paths = _write_repo(
        tmp_path,
        py_files=py_files,
        atlan_yaml=atlan_yaml,
        env_example=env_example,
        manifest_json=manifest_json,
    )
    return scan_all(paths, tmp_path)


def _p025(findings: list) -> list:
    return [f for f in findings if f.rule_id == "P025"]


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_p025_rule_is_blocking() -> None:
    rule = get_rule("P025")
    assert rule.tier == EnforcementTier.BLOCK


def test_p025_rule_is_app_scoped() -> None:
    rule = get_rule("P025")
    assert rule.scope == RuleScope.APP


def test_p025_rule_is_not_autofixable() -> None:
    rule = get_rule("P025")
    assert rule.autofixable is False


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------


def test_p025_no_contract_no_env_is_noop(tmp_path: Path) -> None:
    """No atlan.yaml and no .env.example → no findings (SDK / utility repo)."""
    py = {
        "app/my_app.py": dedent("""\
            from application_sdk.app import App
            class MyApp(App):
                async def run(self, input): ...
        """)
    }
    findings = _run(tmp_path, py)
    assert _p025(findings) == []


def test_p025_no_app_class_found_is_noop(tmp_path: Path) -> None:
    """No App-family subclass in the scanned files → no findings."""
    py = {
        "app/utils.py": dedent("""\
            def helper():
                pass
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: my-app\n",
        env_example="ATLAN_APPLICATION_NAME=my-app\n",
    )
    assert _p025(findings) == []


def test_p025_non_sdk_base_ignored(tmp_path: Path) -> None:
    """A class extending a non-SDK 'App' is not detected (import-provenance guard)."""
    py = {
        "app/my_app.py": dedent("""\
            from some_other_lib import App
            class MyApp(App):
                name = "wrong-lib"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: my-app\n",
        env_example="ATLAN_APPLICATION_NAME=my-app\n",
    )
    assert _p025(findings) == []


# ---------------------------------------------------------------------------
# Silent / fully aligned cases
# ---------------------------------------------------------------------------


def test_p025_all_three_aligned_explicit_name(tmp_path: Path) -> None:
    """Explicit name = '...' matches both atlan.yaml and .env.example → no findings."""
    py = {
        "app/mysql.py": dedent("""\
            from application_sdk.app import App
            class MySQLApp(App):
                name = "mysql"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mysql\ndisplay_name: MySQL\n",
        env_example="ATLAN_APPLICATION_NAME=mysql\n",
    )
    assert _p025(findings) == []


def test_p025_kebab_name_matches_contract_and_env(tmp_path: Path) -> None:
    """No explicit name attr; kebab class name matches contract + env → no findings."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            class HelloWorldApp(App):
                async def run(self, input): ...
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: hello-world-app\n",
        env_example="ATLAN_APPLICATION_NAME=hello-world-app\n",
    )
    assert _p025(findings) == []


def test_p025_only_contract_present_aligned(tmp_path: Path) -> None:
    """Only atlan.yaml present (no .env.example), and it matches → no findings."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.templates import SqlApp
            class MySqlApp(SqlApp):
                name = "mysql"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mysql\n",
    )
    assert _p025(findings) == []


def test_p025_only_env_present_aligned(tmp_path: Path) -> None:
    """Only .env.example present (no atlan.yaml), and it matches → no findings."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            class PostgresApp(App):
                name = "postgres"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        env_example="ATLAN_APPLICATION_NAME=postgres\n",
    )
    assert _p025(findings) == []


# ---------------------------------------------------------------------------
# The MSSQL incident case (the root cause from the ticket)
# ---------------------------------------------------------------------------


def test_p025_mssql_case_fires(tmp_path: Path) -> None:
    """MSSQLMetadataExtractor with no name= → code derives 'mssql-metadata-extractor'
    but atlan.yaml and .env.example say 'mssql' → 2 findings."""
    py = {
        "app/extractor.py": dedent("""\
            from application_sdk.templates import SqlMetadataExtractor
            class MSSQLMetadataExtractor(SqlMetadataExtractor):
                pass
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mssql\ndisplay_name: MSSQL\n",
        env_example="ATLAN_APPLICATION_NAME=mssql\n",
    )
    p025 = _p025(findings)
    assert (
        len(p025) == 2
    ), f"expected 2 findings, got {len(p025)}: {[f.message for f in p025]}"
    messages = [f.message for f in p025]
    assert any("mssql-metadata-extractor" in m and "mssql" in m for m in messages)
    # One finding per source (contract + env)
    assert any("atlan.yaml" in m for m in messages)
    assert any(".env.example" in m for m in messages)


def test_p025_mssql_fixed_with_explicit_name(tmp_path: Path) -> None:
    """Adding name = 'mssql' to MSSQLMetadataExtractor resolves the drift → no findings."""
    py = {
        "app/extractor.py": dedent("""\
            from application_sdk.templates import SqlMetadataExtractor
            class MSSQLMetadataExtractor(SqlMetadataExtractor):
                name = "mssql"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mssql\n",
        env_example="ATLAN_APPLICATION_NAME=mssql\n",
    )
    assert _p025(findings) == []


def test_p025_findings_are_blocking(tmp_path: Path) -> None:
    """P025 findings must be BLOCK-tier."""
    py = {
        "app/extractor.py": dedent("""\
            from application_sdk.templates import SqlMetadataExtractor
            class MSSQLMetadataExtractor(SqlMetadataExtractor):
                pass
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mssql\n",
    )
    p025 = _p025(findings)
    assert p025, "expected at least one P025 finding"
    rule = get_rule("P025")
    assert rule.tier == EnforcementTier.BLOCK


# ---------------------------------------------------------------------------
# Contract-only drift
# ---------------------------------------------------------------------------


def test_p025_contract_drift_only(tmp_path: Path) -> None:
    """atlan.yaml disagrees with code name; no .env.example → 1 finding."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            class PostgresApp(App):
                name = "postgres-connector"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: postgres\n",
    )
    p025 = _p025(findings)
    assert len(p025) == 1
    assert "postgres-connector" in p025[0].message
    assert "postgres" in p025[0].message
    assert "atlan.yaml" in p025[0].message


def test_p025_contract_aligned_env_drifts(tmp_path: Path) -> None:
    """atlan.yaml matches but .env.example disagrees → 1 finding (env only)."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            class MySQLApp(App):
                name = "mysql"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mysql\n",
        env_example="ATLAN_APPLICATION_NAME=mysqldb\n",
    )
    p025 = _p025(findings)
    assert len(p025) == 1
    assert ".env.example" in p025[0].message
    assert "mysqldb" in p025[0].message
    assert "mysql" in p025[0].message


# ---------------------------------------------------------------------------
# SDK template bases (SqlApp, BaseMetadataExtractor, SqlMetadataExtractor, …)
# ---------------------------------------------------------------------------


def test_p025_sql_app_template_detected(tmp_path: Path) -> None:
    """A class extending SqlApp is treated as App-family."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.templates import SqlApp
            class SnowflakeApp(SqlApp):
                name = "snowflake"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: snowflake\n",
        env_example="ATLAN_APPLICATION_NAME=snowflake\n",
    )
    assert _p025(findings) == []


def test_p025_base_metadata_extractor_detected(tmp_path: Path) -> None:
    """A class extending BaseMetadataExtractor is treated as App-family."""
    py = {
        "app/extractor.py": dedent("""\
            from application_sdk.templates import BaseMetadataExtractor
            class TrinoExtractor(BaseMetadataExtractor):
                name = "trino"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: trino\n",
    )
    assert _p025(findings) == []


def test_p025_sql_query_extractor_detected(tmp_path: Path) -> None:
    """A class extending SqlQueryExtractor is treated as App-family."""
    py = {
        "app/extractor.py": dedent("""\
            from application_sdk.templates import SqlQueryExtractor
            class RedshiftQueryExtractor(SqlQueryExtractor):
                name = "redshift"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: redshift\n",
    )
    assert _p025(findings) == []


# ---------------------------------------------------------------------------
# PascalCase → kebab-case name derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("class_name", "expected_kebab"),
    [
        ("MySQLApp", "my-sql-app"),
        ("MSSQLMetadataExtractor", "mssql-metadata-extractor"),
        ("HTTPHandler", "http-handler"),
        ("S3Loader", "s3-loader"),
        ("CsvPipeline", "csv-pipeline"),
        ("HelloWorldApp", "hello-world-app"),
    ],
)
def test_p025_pascal_to_kebab(
    tmp_path: Path, class_name: str, expected_kebab: str
) -> None:
    """Verify the kebab-case derivation matches the SDK's _pascal_to_kebab exactly."""
    py = {
        "app/connector.py": dedent(f"""\
            from application_sdk.app import App
            class {class_name}(App):
                pass
        """)
    }
    # Contract matches → no finding; mismatch → exactly one contract finding
    findings_aligned = _run(
        tmp_path,
        py,
        atlan_yaml=f"name: {expected_kebab}\n",
    )
    assert (
        _p025(findings_aligned) == []
    ), f"{class_name} should derive '{expected_kebab}' and match the contract"
    # Fresh tmp_path for mismatch check
    import tempfile

    with tempfile.TemporaryDirectory() as tmp2:
        findings_drift = _run(
            Path(tmp2),
            py,
            atlan_yaml="name: wrong-name\n",
        )
        p025 = _p025(findings_drift)
        assert (
            len(p025) == 1
        ), f"{class_name} → '{expected_kebab}' should drift against 'wrong-name'"
        assert expected_kebab in p025[0].message


# ---------------------------------------------------------------------------
# Transitive base detection (local intermediate class)
# ---------------------------------------------------------------------------


def test_p025_transitive_same_file_leaf_detected(tmp_path: Path) -> None:
    """Local intermediate base in the same file: leaf subclass is detected."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            class BaseConnector(App):
                pass
            class MySQLConnector(BaseConnector):
                name = "mysql"
        """)
    }
    # MySQLConnector is the leaf (BaseConnector is its base — not a leaf)
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mysql\n",
    )
    assert _p025(findings) == []


def test_p025_transitive_leaf_name_mismatch_fires(tmp_path: Path) -> None:
    """Transitive leaf class with wrong name → 1 finding."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            class BaseConnector(App):
                pass
            class MySQLConnector(BaseConnector):
                pass
        """)
    }
    # MySQLConnector → 'my-sql-connector'; atlan.yaml says 'mysql'
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mysql\n",
    )
    p025 = _p025(findings)
    assert len(p025) == 1
    assert "my-sql-connector" in p025[0].message


# ---------------------------------------------------------------------------
# Multiple leaf App classes (bundle — no-op on comparison)
# ---------------------------------------------------------------------------


def test_p025_multiple_leaf_classes_different_names_noop(tmp_path: Path) -> None:
    """Multiple App-family leaf classes with different names → no comparison findings.

    In a bundle repo, P016 handles entrypoint-name alignment; P025 is a no-op
    for the contract/env comparison to avoid false positives.
    Unresolvable findings are still emitted if present, but there are none here.
    """
    py = {
        "app/crawler.py": dedent("""\
            from application_sdk.app import App
            class CrawlerApp(App):
                name = "crawler"
        """),
        "app/miner.py": dedent("""\
            from application_sdk.app import App
            class MinerApp(App):
                name = "miner"
        """),
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: bundle\n",
        env_example="ATLAN_APPLICATION_NAME=bundle\n",
    )
    # Two leaf classes with different names → comparison skipped → no P025 findings
    assert _p025(findings) == []


def test_p025_multiple_leaf_classes_same_name_aligned(tmp_path: Path) -> None:
    """Multiple App-family leaf classes with the SAME name are treated as one reference."""
    py = {
        "app/extractor.py": dedent("""\
            from application_sdk.app import App
            class MetadataExtractor(App):
                name = "my-app"
        """),
        "app/query.py": dedent("""\
            from application_sdk.app import App
            class QueryExtractor(App):
                name = "my-app"
        """),
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: my-app\n",
    )
    assert _p025(findings) == []


# ---------------------------------------------------------------------------
# Unverifiable (non-literal name=)
# ---------------------------------------------------------------------------


def test_p025_non_literal_name_fires(tmp_path: Path) -> None:
    """Non-literal name= fires an unverifiable finding."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            APP_NAME = "my-app"
            class MyApp(App):
                name = APP_NAME
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: my-app\n",
    )
    p025 = _p025(findings)
    assert len(p025) >= 1
    assert any("non-literal" in f.message for f in p025)


def test_p025_non_literal_name_comparison_skipped(tmp_path: Path) -> None:
    """Non-literal name= → unverifiable finding only; no contract-drift finding."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            APP_NAME = "something-else"
            class MyApp(App):
                name = APP_NAME
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: my-app\n",
        env_example="ATLAN_APPLICATION_NAME=my-app\n",
    )
    p025 = _p025(findings)
    # Should get the unverifiable finding but NOT contract/env drift findings
    # (since we can't determine what the code name is)
    assert all("non-literal" in f.message for f in p025), (
        "With non-literal name=, only unverifiable finding expected, "
        f"got: {[f.message for f in p025]}"
    )


# ---------------------------------------------------------------------------
# Manifest.json fallback (no atlan.yaml)
# ---------------------------------------------------------------------------


def test_p025_manifest_fallback_aligned(tmp_path: Path) -> None:
    """No atlan.yaml; single-EP manifest.json with matching app_name → no findings."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            class PostgresApp(App):
                name = "postgres"
        """)
    }
    manifest = {
        "dag": {"extract": {"app_name": "postgres", "workflow_type": "postgres"}}
    }
    findings = _run(
        tmp_path,
        py,
        manifest_json=manifest,
    )
    assert _p025(findings) == []


def test_p025_manifest_fallback_drift_fires(tmp_path: Path) -> None:
    """No atlan.yaml; single-EP manifest.json with mismatched app_name → 1 finding."""
    py = {
        "app/extractor.py": dedent("""\
            from application_sdk.templates import SqlMetadataExtractor
            class PostgresMetadataExtractor(SqlMetadataExtractor):
                pass
        """)
    }
    manifest = {"dag": {"extract": {"app_name": "postgres"}}}
    findings = _run(
        tmp_path,
        py,
        manifest_json=manifest,
    )
    p025 = _p025(findings)
    assert len(p025) == 1
    assert "postgres-metadata-extractor" in p025[0].message
    assert "postgres" in p025[0].message


# ---------------------------------------------------------------------------
# Inline suppression
# ---------------------------------------------------------------------------


def test_p025_suppressed_by_inline_directive(tmp_path: Path) -> None:
    """# conformance: ignore[P025] on the class line suppresses the finding."""
    py = {
        "app/extractor.py": dedent("""\
            from application_sdk.templates import SqlMetadataExtractor
            # conformance: ignore[P025] legacy class name kept for Argo DAG compat
            class MSSQLMetadataExtractor(SqlMetadataExtractor):
                pass
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mssql\n",
    )
    p025 = _p025(findings)
    assert p025, "expected a (suppressed) P025 finding"
    assert all(f.suppressed for f in p025), "P025 findings should be suppressed"


# ---------------------------------------------------------------------------
# Alias imports
# ---------------------------------------------------------------------------


def test_p025_aliased_app_import_detected(tmp_path: Path) -> None:
    """``from application_sdk.app import App as BaseApp`` is tracked correctly."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App as BaseApp
            class TrinoApp(BaseApp):
                name = "trino"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: trino\n",
    )
    assert _p025(findings) == []


def test_p025_aliased_app_import_drift_fires(tmp_path: Path) -> None:
    """Aliased import with drifting name still emits a finding."""
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.templates import SqlMetadataExtractor as BaseExtractor
            class TrinoExtractor(BaseExtractor):
                pass
        """)
    }
    # code name → trino-extractor; contract says trino
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: trino\n",
    )
    p025 = _p025(findings)
    assert len(p025) == 1
    assert "trino-extractor" in p025[0].message


# ---------------------------------------------------------------------------
# ClassVar annotation form
# ---------------------------------------------------------------------------


def test_p025_classvar_annotation_form(tmp_path: Path) -> None:
    """``name: ClassVar[str] = "mysql"`` is resolved correctly."""
    py = {
        "app/mysql.py": dedent("""\
            from typing import ClassVar
            from application_sdk.app import App
            class MySQLApp(App):
                name: ClassVar[str] = "mysql"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mysql\n",
        env_example="ATLAN_APPLICATION_NAME=mysql\n",
    )
    assert _p025(findings) == []


# ---------------------------------------------------------------------------
# _SDK_APP_BASE_NAMES parity gate
# ---------------------------------------------------------------------------


def test_sdk_base_names_matches_templates_all() -> None:
    """_SDK_APP_BASE_NAMES must equal set(templates.__all__) | {"App"}.

    This test is the deterministic gate that catches a new public template base
    added to application_sdk.templates.__all__ without a matching entry in
    _SDK_APP_BASE_NAMES.  Pure-AST derivation would require importing the SDK
    at check time, which conformance deliberately avoids; the asserted equality
    is the lower-blast-radius alternative.
    """
    from conformance.suite.checks.app_name_alignment._code_app_name import (
        _SDK_APP_BASE_NAMES,
    )

    import application_sdk.templates as _templates

    expected = set(_templates.__all__) | {"App"}
    assert _SDK_APP_BASE_NAMES == frozenset(expected), (
        f"_SDK_APP_BASE_NAMES is out of sync with application_sdk.templates.__all__.\n"
        f"  In __all__ but not in _SDK_APP_BASE_NAMES: {expected - _SDK_APP_BASE_NAMES}\n"
        f"  In _SDK_APP_BASE_NAMES but not in __all__: {_SDK_APP_BASE_NAMES - expected}"
    )


# ---------------------------------------------------------------------------
# Empty-string name= falls back to kebab
# ---------------------------------------------------------------------------


def test_p025_empty_string_name_falls_back_to_kebab(tmp_path: Path) -> None:
    """name = '' is treated as 'no name' and kebab-cased from the class name.

    The SDK base class declares name: ClassVar[str] = '' (base.py:476), so a
    subclass that re-asserts the default with name = '' must get the same
    runtime behaviour: cls.name or _pascal_to_kebab(cls.__name__) → kebab.
    Without this fix the static check would compare '' against atlan.yaml and
    emit a confusing "App name '' (code) does not match ..." finding.
    """
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            class MySQLApp(App):
                name = ""
        """)
    }
    # kebab of MySQLApp → my-sql-app; contract matches → no findings
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: my-sql-app\n",
        env_example="ATLAN_APPLICATION_NAME=my-sql-app\n",
    )
    assert _p025(findings) == []

    # kebab → my-sql-app; contract says wrong-name → 1 finding naming my-sql-app
    import tempfile

    with tempfile.TemporaryDirectory() as tmp2:
        findings_drift = _run(
            Path(tmp2),
            py,
            atlan_yaml="name: wrong-name\n",
        )
        p025 = _p025(findings_drift)
        assert len(p025) == 1, f"expected 1 finding, got {len(p025)}"
        assert "my-sql-app" in p025[0].message


# ---------------------------------------------------------------------------
# Transitive ancestor name propagation (intermediate base declares name=)
# ---------------------------------------------------------------------------


def test_p025_transitive_intermediate_base_name_propagates(tmp_path: Path) -> None:
    """When an intermediate base declares name = '...', the leaf inherits it.

    Runtime: cls.name resolves via MRO — LeafClass.name → IntermediateBase.name
    = 'mysql'.  The static check must walk scanned ancestors to match this, or
    it would fall back to kebab(LeafClass) and emit a false-positive drift
    finding.
    """
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            class BaseConnector(App):
                name = "mysql"
            class MySQLConnector(BaseConnector):
                pass
        """)
    }
    # MySQLConnector inherits name="mysql" via MRO; contract matches → no findings
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mysql\n",
        env_example="ATLAN_APPLICATION_NAME=mysql\n",
    )
    assert (
        _p025(findings) == []
    ), "Expected no findings: leaf inherits name='mysql' from intermediate base"

    # Contract says wrong-name → 1 finding with the inherited "mysql" name
    import tempfile

    with tempfile.TemporaryDirectory() as tmp2:
        findings_drift = _run(
            Path(tmp2),
            py,
            atlan_yaml="name: wrong-name\n",
        )
        p025 = _p025(findings_drift)
        assert len(p025) == 1
        assert "mysql" in p025[0].message


# ---------------------------------------------------------------------------
# Quoted env var values (python-dotenv strips quotes at runtime)
# ---------------------------------------------------------------------------


def test_p025_quoted_env_double_quotes_noop(tmp_path: Path) -> None:
    """ATLAN_APPLICATION_NAME="mssql" (double-quoted) must match code name 'mssql'."""
    py = {
        "app/extractor.py": dedent("""\
            from application_sdk.templates import SqlMetadataExtractor
            class MSSQLMetadataExtractor(SqlMetadataExtractor):
                name = "mssql"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mssql\n",
        env_example='ATLAN_APPLICATION_NAME="mssql"\n',
    )
    assert (
        _p025(findings) == []
    ), "Double-quoted env value should be stripped before comparison"


def test_p025_quoted_env_single_quotes_noop(tmp_path: Path) -> None:
    """ATLAN_APPLICATION_NAME='mssql' (single-quoted) must match code name 'mssql'."""
    py = {
        "app/extractor.py": dedent("""\
            from application_sdk.templates import SqlMetadataExtractor
            class MSSQLMetadataExtractor(SqlMetadataExtractor):
                name = "mssql"
        """)
    }
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: mssql\n",
        env_example="ATLAN_APPLICATION_NAME='mssql'\n",
    )
    assert (
        _p025(findings) == []
    ), "Single-quoted env value should be stripped before comparison"


def test_p025_empty_name_intermediate_shadows_grandparent(tmp_path: Path) -> None:
    """Intermediate ``name = ""`` terminates the ancestor BFS; grandparent ignored.

    Chain: GrandparentApp(App) with name="foo" → IntermediateBase with name=""
    → LeafClass with no name attr.

    Runtime MRO:
      LeafClass.name   → IntermediateBase.name = ""
      "" or kebab(LeafClass) → "leaf-class"

    The empty-literal on IntermediateBase shadows GrandparentApp's "foo" and
    the runtime falls back to kebab(leaf).  Without this fix the BFS would walk
    past the empty-literal intermediate and find "foo", producing a
    false-positive drift finding against a contract that says "leaf-class".
    """
    py = {
        "app/connector.py": dedent("""\
            from application_sdk.app import App
            class GrandparentApp(App):
                name = "foo"
            class IntermediateBase(GrandparentApp):
                name = ""
            class LeafClass(IntermediateBase):
                pass
        """)
    }
    # Effective name = kebab("LeafClass") = "leaf-class"; contract matches → no findings
    findings = _run(
        tmp_path,
        py,
        atlan_yaml="name: leaf-class\n",
        env_example="ATLAN_APPLICATION_NAME=leaf-class\n",
    )
    assert _p025(findings) == [], (
        "Empty-literal intermediate should terminate BFS; 'foo' from grandparent "
        "must not be inherited by the leaf"
    )

    # Contract says "foo" (the grandparent value) → 1 finding (code=leaf-class)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp2:
        findings_drift = _run(
            Path(tmp2),
            py,
            atlan_yaml="name: foo\n",
        )
        p025 = _p025(findings_drift)
        assert len(p025) == 1, f"expected 1 finding, got {len(p025)}"
        assert "leaf-class" in p025[0].message
