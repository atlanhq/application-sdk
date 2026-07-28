"""Meta-tests for the T023/T024 e2e generated-harness checks.

T023 catches e2e scaffold hand-written under ``tests/`` that the contract
toolkit already generates from ``contract/app.pkl`` — identity attrs on a
harness subclass, ``CredentialBody`` subclasses, ``MustacheSubstitutions``
subclasses.  T024 catches a collectable e2e test class that never declares
``mode`` and so inherits ``BaseE2ETest``'s ``RunMode.DIRECT`` default, which
routes extraction away from the CI-side worker the reusable e2e job starts.

Tests cover both fire paths, the canonical ``atlan-mysql-app`` shape as the
clean case, transitive inheritance through generated and in-repo bases,
non-harness classes, and inline suppression.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.e2e_generated_harness import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# What the toolkit emits for atlan-mysql-app.
_GENERATED_BASE = """\
# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
from application_sdk.testing.e2e import SQLAppE2ETest


class MysqlGeneratedE2EBase(SQLAppE2ETest):
    connector_short_name = "mysql"
    argo_package_name = "@atlan/mysql"
    argo_template_name = "atlan-mysql"
    app_service_url = "http://mysql.mysql-app.svc.cluster.local"
    connection_type = "mysql"
"""

# The canonical connector test: generated base + connector-specific knobs only.
_CANONICAL_TEST = """\
from application_sdk.testing.e2e import RunMode

from app.generated._e2e_base import MysqlGeneratedE2EBase
from app.generated._e2e_credential import MysqlAgentCredentialBody


class TestMySQLFullDAG(MysqlGeneratedE2EBase):
    mode = RunMode.AGENT
    include_filter = r"^def\\.e2e_main$"
    expected_min_asset_counts = {"Database": 1, "Table": 2}

    def _credential_body(self) -> MysqlAgentCredentialBody:
        return MysqlAgentCredentialBody(name="x")
"""

# The shape the SDR fleet sweep produced.
_HANDWRITTEN_TEST = """\
from application_sdk.testing.e2e import RunMode, SQLAppE2ETest
from application_sdk.testing.e2e.credential import CredentialBody
from application_sdk.testing.e2e.substitutions import SQLMustacheSubstitutions
from pydantic import Field


class HiveMustacheSubstitutions(SQLMustacheSubstitutions):
    preflight_check: str = Field(default="", alias="{{preflight-check}}")


class HiveAgentCredentialBody(CredentialBody):
    name: str = Field(alias="name")
    auth_type: str = Field(default="basic", alias="authType")


class TestHiveFullDAG(SQLAppE2ETest):
    connector_short_name = "hive"
    argo_package_name = "@atlan/hive-miner"
    argo_template_name = "atlan-hive"
    connection_type = "hive"
    connection_category = "database"
    mode = RunMode.AGENT
    app_service_url = "http://hive.hive-app.svc.cluster.local:8000"
"""


def _repo(
    tmp_path: Path,
    *,
    tests: dict[str, str] | None = None,
    generated: dict[str, str] | None = None,
) -> Path:
    root = tmp_path
    if tests:
        for rel, text in tests.items():
            path = root / "tests" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if generated:
        for rel, text in generated.items():
            path = root / "app" / "generated" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return root


def _scan(root: Path) -> list:
    return scan_all(discover(root), root)


def _ids(findings: list) -> list[str]:
    return [f.rule_id for f in findings if not f.suppressed]


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_t023_rule_metadata() -> None:
    rule = get_rule("T023")
    assert rule.name == "E2EHarnessScaffoldHandWritten"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.since == "0.18.0"
    assert rule.category == "e2e-ci"
    assert rule.orthogonal_gate == "pkl-eval"
    assert rule.rationale.strip()


def test_t024_rule_metadata() -> None:
    rule = get_rule("T024")
    assert rule.name == "E2ERunModeUnset"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.rationale.strip()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_walks_tests_tree(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        tests={"e2e/test_a.py": "", "unit/test_b.py": "", "e2e/README.md": ""},
    )
    names = {p.name for p in discover(root)}
    assert names == {"test_a.py", "test_b.py"}


def test_discover_empty_without_tests_dir(tmp_path: Path) -> None:
    assert discover(tmp_path) == []


# ---------------------------------------------------------------------------
# T023 — hand-written scaffold
# ---------------------------------------------------------------------------


def test_t023_fires_on_handwritten_identity_attrs(tmp_path: Path) -> None:
    root = _repo(tmp_path, tests={"e2e/test_hive_full_dag.py": _HANDWRITTEN_TEST})
    findings = [f for f in _scan(root) if f.rule_id == "T023"]
    identity = [f for f in findings if "identity attrs" in f.message]
    assert len(identity) == 1
    assert "connector_short_name" in identity[0].message
    assert "connection_category" in identity[0].message
    assert "MysqlGeneratedE2EBase" not in identity[0].message  # generic guidance


def test_t023_fires_on_handwritten_credential_body(tmp_path: Path) -> None:
    root = _repo(tmp_path, tests={"e2e/test_hive_full_dag.py": _HANDWRITTEN_TEST})
    findings = [f for f in _scan(root) if f.rule_id == "T023"]
    cred = [f for f in findings if "CredentialBody subclass" in f.message]
    assert len(cred) == 1
    assert "_e2e_credential.py" in cred[0].message


def test_t023_fires_on_handwritten_substitutions(tmp_path: Path) -> None:
    root = _repo(tmp_path, tests={"e2e/test_hive_full_dag.py": _HANDWRITTEN_TEST})
    findings = [f for f in _scan(root) if f.rule_id == "T023"]
    subs = [f for f in findings if "SQLMustacheSubstitutions subclass" in f.message]
    assert len(subs) == 1
    assert "_e2e_substitutions.py" in subs[0].message


def test_t023_clean_on_canonical_generated_shape(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        tests={"e2e/test_mysql_full_dag.py": _CANONICAL_TEST},
        generated={"_e2e_base.py": _GENERATED_BASE},
    )
    assert "T023" not in _ids(_scan(root))


def test_t023_ignores_non_harness_classes(tmp_path: Path) -> None:
    """A plain helper that happens to name one of the attrs is not a harness."""
    helper = """\
class Config:
    connector_short_name = "mysql"
    app_service_url = "http://x"
"""
    root = _repo(tmp_path, tests={"unit/test_helper.py": helper})
    assert "T023" not in _ids(_scan(root))


def test_t023_fires_through_an_in_repo_intermediate_base(tmp_path: Path) -> None:
    """A shared in-repo base still resolves to the SDK harness."""
    shared = """\
from application_sdk.testing.e2e import BaseE2ETest


class BaseFullDAGE2ETest(BaseE2ETest):
    connection_name_prefix = "e2e-full-ci"
"""
    leaf = """\
from application_sdk.testing.e2e import RunMode

from .base import BaseFullDAGE2ETest


class TestSnowflakeFullDAG(BaseFullDAGE2ETest):
    connector_short_name = "snowflake"
    argo_package_name = "@atlan/snowflake"
    mode = RunMode.AGENT
"""
    root = _repo(tmp_path, tests={"e2e/base.py": shared, "e2e/test_sf.py": leaf})
    identity = [
        f for f in _scan(root) if f.rule_id == "T023" and "identity attrs" in f.message
    ]
    assert len(identity) == 1
    assert "TestSnowflakeFullDAG" in identity[0].message


def test_t023_suppressed_inline(tmp_path: Path) -> None:
    suppressed = _HANDWRITTEN_TEST.replace(
        '    connector_short_name = "hive"',
        "    # conformance: ignore[T023] intentional: negative-path fixture\n"
        '    connector_short_name = "hive"',
    )
    root = _repo(tmp_path, tests={"e2e/test_hive_full_dag.py": suppressed})
    identity = [
        f
        for f in scan_all(discover(root), root)
        if f.rule_id == "T023" and "identity attrs" in f.message
    ]
    assert len(identity) == 1
    assert identity[0].suppressed is True


# ---------------------------------------------------------------------------
# T024 — run mode unset
# ---------------------------------------------------------------------------


def test_t024_fires_when_mode_is_never_declared(tmp_path: Path) -> None:
    test_src = """\
from application_sdk.testing.e2e import SQLAppE2ETest


class TestThingFullDAG(SQLAppE2ETest):
    include_filter = "x"
"""
    root = _repo(tmp_path, tests={"e2e/test_thing.py": test_src})
    findings = [f for f in _scan(root) if f.rule_id == "T024"]
    assert len(findings) == 1
    assert "RunMode.DIRECT" in findings[0].message
    assert "TestThingFullDAG" in findings[0].message


def test_t024_clean_when_mode_declared_agent(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        tests={"e2e/test_mysql_full_dag.py": _CANONICAL_TEST},
        generated={"_e2e_base.py": _GENERATED_BASE},
    )
    assert "T024" not in _ids(_scan(root))


def test_t024_clean_when_mode_declared_direct(tmp_path: Path) -> None:
    """An explicit tier-5 DIRECT run is a legal, visible choice."""
    test_src = """\
from application_sdk.testing.e2e import RunMode, SQLAppE2ETest


class TestThingDirect(SQLAppE2ETest):
    mode = RunMode.DIRECT
"""
    root = _repo(tmp_path, tests={"e2e/test_thing.py": test_src})
    assert "T024" not in _ids(_scan(root))


def test_t024_clean_when_an_in_repo_base_declares_mode(tmp_path: Path) -> None:
    shared = """\
from application_sdk.testing.e2e import BaseE2ETest, RunMode


class BaseFullDAGE2ETest(BaseE2ETest):
    mode = RunMode.AGENT
"""
    leaf = """\
from .base import BaseFullDAGE2ETest


class TestThingFullDAG(BaseFullDAGE2ETest):
    expected_min_asset_counts = {"Table": 1}
"""
    root = _repo(tmp_path, tests={"e2e/base.py": shared, "e2e/test_thing.py": leaf})
    assert "T024" not in _ids(_scan(root))


def test_t024_ignores_non_collectable_harness_bases(tmp_path: Path) -> None:
    """An abstract in-repo base is not a pytest-collected class."""
    shared = """\
from application_sdk.testing.e2e import BaseE2ETest


class SharedHarness(BaseE2ETest):
    connection_name_prefix = "e2e"
"""
    root = _repo(tmp_path, tests={"e2e/base.py": shared})
    assert "T024" not in _ids(_scan(root))


def test_t024_ignores_a_mode_local_to_a_method(tmp_path: Path) -> None:
    test_src = """\
from application_sdk.testing.e2e import SQLAppE2ETest


class TestThingFullDAG(SQLAppE2ETest):
    def test_run(self) -> None:
        mode = "agent"
        assert mode
"""
    root = _repo(tmp_path, tests={"e2e/test_thing.py": test_src})
    assert "T024" in _ids(_scan(root))


def test_t024_suppressed_inline(tmp_path: Path) -> None:
    test_src = """\
from application_sdk.testing.e2e import SQLAppE2ETest


# conformance: ignore[T024] mode is parametrised from E2E_RUN_MODE at setup
class TestThingFullDAG(SQLAppE2ETest):
    include_filter = "x"
"""
    root = _repo(tmp_path, tests={"e2e/test_thing.py": test_src})
    findings = [f for f in scan_all(discover(root), root) if f.rule_id == "T024"]
    assert len(findings) == 1
    assert findings[0].suppressed is True
