"""Meta-tests for the T017 e2e-agent-spec check.

T017 catches an e2e harness ``agent_spec`` override that hard-codes the
extract-node queue (a run-id-keyed ``AgentSpec`` that neither reads
``ATLAN_DEPLOYMENT_NAME`` nor calls ``super().agent_spec()``) instead of
inheriting the per-leg deployment queue. It is the harness-side complement to
T016 — the pair that, split, produced the atlan-metabase-app "No Workers
Running" regression.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.e2e_agent_spec import discover, scan_path, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ── Rule metadata ────────────────────────────────────────────────────────────


def test_t017_rule_metadata() -> None:
    rule = get_rule("T017")
    assert rule.name == "E2EAgentSpecPinsQueue"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.category == "e2e-ci"


# ── Fixtures ─────────────────────────────────────────────────────────────────

_HARDCODED_FSTRING = (
    "class T(Base):\n"
    "    def agent_spec(self) -> AgentSpec:\n"
    '        return AgentSpec(agent_name=f"metabase-e2e-full-ci-{self.run_id}")\n'
)
_HARDCODED_PLAIN = (
    "class T(Base):\n"
    "    def agent_spec(self):\n"
    '        return AgentSpec(agent_name="pinned-name")\n'
)
_GUARDED_SUPER = (
    "class T(Base):\n"
    "    def agent_spec(self):\n"
    "        if os.environ.get('ATLAN_APPLICATION_NAME') and os.environ.get(\n"
    "            'ATLAN_DEPLOYMENT_NAME'\n"
    "        ):\n"
    "            return super().agent_spec()\n"
    '        return AgentSpec(agent_name=f"metabase-e2e-full-ci-{self.run_id}")\n'
)


# ── Positive detections ──────────────────────────────────────────────────────


def test_flags_hardcoded_fstring_agent_spec() -> None:
    findings = scan_text(_HARDCODED_FSTRING, "tests/e2e/test_x.py")
    assert [f.rule_id for f in findings] == ["T017"]
    assert findings[0].line == 2  # the def line


def test_flags_hardcoded_plain_string_agent_spec() -> None:
    findings = scan_text(_HARDCODED_PLAIN, "tests/e2e/test_x.py")
    assert [f.rule_id for f in findings] == ["T017"]


def test_flags_positional_hardcoded_agent_spec() -> None:
    # agent_name is AgentSpec's first field, so the positional form
    # AgentSpec("x") / AgentSpec(f"...{run_id}") is equally hard-coded and must
    # be flagged too — not just the agent_name= keyword form.
    for body in (
        '        return AgentSpec("pinned-name")\n',
        '        return AgentSpec(f"metabase-e2e-full-ci-{self.run_id}")\n',
    ):
        text = "class T(Base):\n    def agent_spec(self):\n" + body
        assert [f.rule_id for f in scan_text(text, "tests/e2e/test_x.py")] == [
            "T017"
        ], body


# ── Compliant forms (no finding) ─────────────────────────────────────────────


def test_passes_no_agent_spec_override() -> None:
    text = "class T(Base):\n    connector_short_name = 'metabase'\n"
    assert scan_text(text, "tests/e2e/test_x.py") == []


def test_passes_guarded_override_that_defers_to_super() -> None:
    assert scan_text(_GUARDED_SUPER, "tests/e2e/test_x.py") == []


def test_passes_override_reading_deployment_env() -> None:
    text = (
        "class T(Base):\n"
        "    def agent_spec(self):\n"
        "        dep = os.environ['ATLAN_DEPLOYMENT_NAME']\n"
        '        return AgentSpec(agent_name=f"metabase-{dep}")\n'
    )
    assert scan_text(text, "tests/e2e/test_x.py") == []


def test_passes_direct_mode_returning_none() -> None:
    text = "class T(Base):\n    def agent_spec(self):\n        return None\n"
    assert scan_text(text, "tests/e2e/test_x.py") == []


def test_noop_on_syntax_error() -> None:
    assert scan_text("class T(:\n  def", "tests/e2e/test_x.py") == []


# ── Inline suppression ───────────────────────────────────────────────────────


def test_suppression_directive_on_def_line() -> None:
    text = (
        "class T(Base):\n"
        "    def agent_spec(self):  # conformance: ignore[T017] single-leg suite\n"
        '        return AgentSpec(agent_name=f"metabase-e2e-full-ci-{self.run_id}")\n'
    )
    findings = scan_text(text, "tests/e2e/test_x.py")
    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert findings[0].suppression_justification == "single-leg suite"


# ── Discovery + scan_path integration ────────────────────────────────────────


def test_discover_walks_tests_tree_and_scan_path(tmp_path: Path) -> None:
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    test_file = e2e_dir / "test_conn_e2e.py"
    test_file.write_text(_HARDCODED_FSTRING, encoding="utf-8")

    discovered = discover(tmp_path)
    assert test_file in discovered

    findings = scan_path(test_file, tmp_path)
    assert [f.rule_id for f in findings] == ["T017"]
    assert findings[0].file == "tests/e2e/test_conn_e2e.py"


def test_discover_empty_without_tests_dir(tmp_path: Path) -> None:
    assert discover(tmp_path) == []
