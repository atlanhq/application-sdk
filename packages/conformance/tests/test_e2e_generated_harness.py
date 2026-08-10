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


def test_t023_fires_through_an_intermediate_model_subclass(tmp_path: Path) -> None:
    """A hand-written model hides behind one hop of repo-local indirection.

    Matching direct base names only let
    ``class CustomCredential(BaseCredential)`` — where
    ``BaseCredential(CredentialBody)`` lives in a neighbouring module — evade
    the rule entirely, though the model is exactly the hand-written scaffold
    T023 exists to catch.  Ancestry now resolves transitively through the same
    resolver the harness branches use.
    """
    base = """\
from application_sdk.testing.e2e.credential import CredentialBody


class BaseCredential(CredentialBody):
    pass
"""
    leaf = """\
from pydantic import Field

from .cred_base import BaseCredential


class CustomCredential(BaseCredential):
    name: str = Field(alias="name")
"""
    root = _repo(tmp_path, tests={"e2e/cred_base.py": base, "e2e/test_cred.py": leaf})
    cred = [
        f
        for f in _scan(root)
        if f.rule_id == "T023" and "CredentialBody subclass" in f.message
    ]
    # Both fire: the intermediate base is itself a hand-written CredentialBody
    # subclass under tests/ (caught on its direct base, as before), and the
    # leaf is now caught through the transitive resolution the direct-bases
    # match missed.
    assert {f.file for f in cred} == {
        "tests/e2e/cred_base.py",
        "tests/e2e/test_cred.py",
    }
    assert any("CustomCredential" in f.message for f in cred)


def test_t023_fires_through_an_aliased_model_import(tmp_path: Path) -> None:
    """`from ... import CredentialBody as Body` names the referent, not the spelling."""
    leaf = """\
from application_sdk.testing.e2e.credential import CredentialBody as Body
from pydantic import Field


class CustomCredential(Body):
    name: str = Field(alias="name")
"""
    root = _repo(tmp_path, tests={"e2e/test_cred.py": leaf})
    cred = [
        f
        for f in _scan(root)
        if f.rule_id == "T023" and "CredentialBody subclass" in f.message
    ]
    assert len(cred) == 1
    assert "CustomCredential" in cred[0].message


def test_t023_ignores_an_unrelated_same_named_base(tmp_path: Path) -> None:
    """An in-repo ``Body`` that is no generated model must not be graded as one."""
    base = """\
class Body:
    pass
"""
    leaf = """\
from .things import Body


class CustomCredential(Body):
    pass
"""
    root = _repo(tmp_path, tests={"e2e/things.py": base, "e2e/test_cred.py": leaf})
    assert "T023" not in _ids(_scan(root))


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


def test_t024_fires_through_an_aliased_generated_base_import(tmp_path: Path) -> None:
    """`from ... import FooGeneratedE2EBase as Base` must still reach the harness.

    Resolving a base purely by the bare identifier in `class T(Base):` matched
    the *spelling*, not the referent: an aliased import of a generated base
    resolved to nothing, the subclass was graded as a plain non-harness class,
    and T024 reported green on a class with no `mode` anywhere. Alias bindings
    are now resolved before the bare-name fallback.
    """
    test_src = """\
from app.generated._e2e_base import MysqlGeneratedE2EBase as Base


class TestMySQLFullDAG(Base):
    include_filter = "x"
"""
    root = _repo(
        tmp_path,
        tests={"e2e/test_mysql_full_dag.py": test_src},
        generated={"_e2e_base.py": _GENERATED_BASE},
    )
    findings = [f for f in _scan(root) if f.rule_id == "T024"]
    assert len(findings) == 1
    assert "TestMySQLFullDAG" in findings[0].message


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


# ---------------------------------------------------------------------------
# Regression: evidence must come from the scope being graded
# ---------------------------------------------------------------------------


def test_scan_survives_non_utf8_test_file(tmp_path: Path) -> None:
    """One non-UTF-8 file under tests/ must not abort the whole run."""
    root = _repo(
        tmp_path,
        generated={"_e2e_base.py": _GENERATED_BASE},
        tests={"e2e/test_full_dag.py": _CANONICAL_TEST},
    )
    (root / "tests" / "e2e" / "test_bin.py").write_bytes(b"\xff\xfe class X: pass\n")
    assert _ids(_scan(root)) == []


def test_t024_not_silenced_by_an_unrelated_same_named_class(tmp_path: Path) -> None:
    """A bare-name index let an unrelated class shadow the real harness.

    ``discover()`` returns sorted paths, so ``aaa_unrelated`` indexed first and
    won the key; the genuine violation was then never evaluated. Recurring
    ``TestFullDag``/``TestExtraction`` names across a test tree are ordinary.
    """
    genuine = """\
from app.generated._e2e_base import MysqlGeneratedE2EBase


class TestFullDag(MysqlGeneratedE2EBase):
    include_filter = r"^def\\.e2e_main$"
"""
    root = _repo(
        tmp_path,
        generated={"_e2e_base.py": _GENERATED_BASE},
        tests={
            "aaa_unrelated/test_full_dag.py": "class TestFullDag:\n    pass\n",
            "e2e/test_full_dag.py": genuine,
        },
    )
    assert "T024" in _ids(_scan(root))


def test_t024_not_flagged_in_a_file_pytest_never_collects(tmp_path: Path) -> None:
    """A shared harness base in tests/e2e/helpers.py is not collected.

    It only matters through a leaf subclass that *is* collected, and grading it
    there is a false positive.
    """
    helper = """\
from app.generated._e2e_base import MysqlGeneratedE2EBase


class TestSharedBase(MysqlGeneratedE2EBase):
    include_filter = r"^def\\.e2e_main$"
"""
    root = _repo(
        tmp_path,
        generated={"_e2e_base.py": _GENERATED_BASE},
        tests={"e2e/helpers.py": helper},
    )
    assert "T024" not in _ids(_scan(root))


def test_t023_anchors_in_the_file_it_reports(tmp_path: Path) -> None:
    """The finding's line must belong to the file it names.

    Routing the visited class through a shared bare-name index could anchor a
    T023 at another file's AST node while reporting this file's path.
    """
    handwritten = """\
from application_sdk.testing.e2e import SQLAppE2ETest


class TestFullDag(SQLAppE2ETest):
    connector_short_name = "mysql"
"""
    root = _repo(
        tmp_path,
        tests={
            "aaa_unrelated/test_full_dag.py": "class TestFullDag:\n    pass\n",
            "e2e/test_full_dag.py": handwritten,
        },
    )
    t023 = [f for f in _scan(root) if f.rule_id == "T023"]
    assert len(t023) == 1
    assert t023[0].file == "tests/e2e/test_full_dag.py"
    target = (root / t023[0].file).read_text(encoding="utf-8").splitlines()
    assert "connector_short_name" in target[t023[0].line - 1]


def test_t024_not_fired_by_ambiguity_among_non_harness_classes(
    tmp_path: Path,
) -> None:
    """Report-on-ambiguity needs a floor innocent classes cannot trip.

    Two unrelated files defining `class Helper` must not turn an ordinary test
    class into a graded e2e harness.
    """
    root = _repo(
        tmp_path,
        tests={
            "a/helpers.py": "class Helper:\n    pass\n",
            "b/helpers.py": "class Helper:\n    pass\n",
            "e2e/test_thing.py": (
                "from tests.b.helpers import Helper\n"
                "\n"
                "class TestSomething(Helper):\n"
                "    def test_foo(self):\n"
                "        assert True\n"
            ),
        },
    )
    assert _ids(_scan(root)) == []


def test_t024_still_fires_when_an_ambiguous_candidate_could_be_a_harness(
    tmp_path: Path,
) -> None:
    """The floor must not disarm the bias where it matters."""
    root = _repo(
        tmp_path,
        tests={
            "a/helpers.py": "class Shared:\n    pass\n",
            "b/helpers.py": (
                "from application_sdk.testing.e2e import BaseE2ETest\n"
                "\n"
                "class Shared(BaseE2ETest):\n"
                "    connector_short_name = 'x'\n"
            ),
            "e2e/test_thing.py": (
                "from tests.b.helpers import Shared\n"
                "\n"
                "class TestSomething(Shared):\n"
                "    def test_foo(self):\n"
                "        assert True\n"
            ),
        },
    )
    assert "T024" in _ids(_scan(root))


def test_t024_silent_when_the_ambiguous_ancestor_declares_mode(
    tmp_path: Path,
) -> None:
    """The floor must not grade a class and then deny it its own ancestor.

    `ambiguous` answers _is_harness_class's question, not _declares_mode's.
    """
    root = _repo(
        tmp_path,
        tests={
            "a/helpers.py": "class Shared:\n    pass\n",
            "b/helpers.py": (
                "from application_sdk.testing.e2e import BaseE2ETest, RunMode\n"
                "\n"
                "class Shared(BaseE2ETest):\n"
                "    mode = RunMode.AGENT\n"
            ),
            "e2e/test_thing.py": (
                "from tests.b.helpers import Shared\n"
                "\n"
                "class TestSomething(Shared):\n"
                "    def test_foo(self):\n"
                "        assert True\n"
            ),
        },
    )
    assert "T024" not in _ids(_scan(root))
