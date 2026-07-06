"""Meta-tests for the T005–T009 assertion-quality / silent-non-execution checks (BLDX-1400).

T005 catches a test that runs without ever checking an outcome (no recognised
assertion). T006 catches a placeholder stub. T007 catches a test whose only
assertion is a constant-true expression that can never fail. T008 catches a
test file that pytest never collects because its name doesn't match the
default collection glob. T009 catches an unconditional module-level
``pytest.skip(..., allow_module_level=True)``.
"""

from __future__ import annotations

from conformance.suite.checks.test_quality import scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ── Rule metadata ────────────────────────────────────────────────────────────


def test_t005_rule_metadata() -> None:
    rule = get_rule("T005")
    assert rule.name == "AssertionFreeTest"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.BOTH
    assert rule.autofixable is False
    assert rule.since == "0.12.0"
    assert rule.category == "test-assertion-quality"


def test_t006_rule_metadata() -> None:
    rule = get_rule("T006")
    assert rule.name == "EmptyTestBody"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.BOTH


def test_t007_rule_metadata() -> None:
    rule = get_rule("T007")
    assert rule.name == "VacuousAssertion"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.BOTH


def test_t008_rule_metadata() -> None:
    rule = get_rule("T008")
    assert rule.name == "UncollectableTestFile"
    assert rule.category == "test-collection"


def test_t009_rule_metadata() -> None:
    rule = get_rule("T009")
    assert rule.name == "UnconditionalModuleSkip"
    assert rule.category == "test-collection"


# ── T005: assertion-free test ────────────────────────────────────────────────


def test_t005_fires_on_test_with_no_assertion() -> None:
    findings = scan_text(
        "def test_extracts_users():\n    result = extract_users(client)\n",
        "tests/unit/test_foo.py",
    )
    assert [f.rule_id for f in findings] == ["T005"]
    assert "test_extracts_users" in findings[0].message


def test_t005_fires_on_class_method_with_no_assertion() -> None:
    findings = scan_text(
        "class TestFoo:\n    def test_bar(self):\n        compute()\n",
        "tests/unit/test_foo.py",
    )
    assert [f.rule_id for f in findings] == ["T005"]
    assert "TestFoo.test_bar" in findings[0].message


def test_t005_silent_on_bare_assert() -> None:
    findings = scan_text(
        "def test_foo():\n    x = compute()\n    assert x == 1\n",
        "tests/unit/test_foo.py",
    )
    assert findings == []


def test_t005_silent_on_pytest_raises() -> None:
    findings = scan_text(
        "import pytest\ndef test_foo():\n    with pytest.raises(ValueError):\n        compute()\n",
        "tests/unit/test_foo.py",
    )
    assert findings == []


def test_t005_silent_on_mock_assert_called() -> None:
    findings = scan_text(
        "def test_foo(mocker):\n"
        "    m = mocker.patch('x')\n"
        "    do_thing()\n"
        "    m.assert_called_once()\n",
        "tests/unit/test_foo.py",
    )
    assert findings == []


def test_t005_silent_on_unittest_style_assert() -> None:
    findings = scan_text(
        "class TestFoo(TestCase):\n"
        "    def test_bar(self):\n"
        "        self.assertEqual(compute(), 1)\n",
        "tests/unit/test_foo.py",
    )
    assert findings == []


def test_t005_silent_on_scenario_helper() -> None:
    findings = scan_text(
        "def test_foo(scenario):\n    scenario.equals(actual, expected)\n",
        "tests/integration/test_foo.py",
    )
    assert findings == []


def test_t005_suppressed_with_directive() -> None:
    """The suppression anchors on the `def` line — T005's finding location."""
    findings = scan_text(
        "# conformance: ignore[T005] intentional: exercises no-raise path only\n"
        "def test_foo():\n"
        "    compute()\n",
        "tests/unit/test_foo.py",
    )
    assert len(findings) == 1
    assert findings[0].suppressed is True


# ── T006: empty/stub body ────────────────────────────────────────────────────


def test_t006_fires_on_pass_only_body() -> None:
    findings = scan_text("def test_foo():\n    pass\n", "tests/unit/test_foo.py")
    assert [f.rule_id for f in findings] == ["T006"]


def test_t006_fires_on_ellipsis_body() -> None:
    findings = scan_text("def test_foo():\n    ...\n", "tests/unit/test_foo.py")
    assert [f.rule_id for f in findings] == ["T006"]


def test_t006_fires_on_docstring_only_body() -> None:
    findings = scan_text(
        'def test_foo():\n    """TODO: implement."""\n', "tests/unit/test_foo.py"
    )
    assert [f.rule_id for f in findings] == ["T006"]


def test_t006_not_t005_for_stub() -> None:
    """A stub body is reported once, as T006 — never also as T005."""
    findings = scan_text("def test_foo():\n    pass\n", "tests/unit/test_foo.py")
    assert len(findings) == 1
    assert findings[0].rule_id == "T006"


# ── T007: vacuous assertion ──────────────────────────────────────────────────


def test_t007_fires_on_assert_true() -> None:
    findings = scan_text(
        "def test_foo():\n    compute()\n    assert True\n", "tests/unit/test_foo.py"
    )
    assert [f.rule_id for f in findings] == ["T007"]


def test_t007_fires_on_assert_constant_int() -> None:
    findings = scan_text(
        "def test_foo():\n    compute()\n    assert 1\n", "tests/unit/test_foo.py"
    )
    assert [f.rule_id for f in findings] == ["T007"]


def test_t007_silent_when_a_real_assertion_also_present() -> None:
    findings = scan_text(
        "def test_foo():\n"
        "    x = compute()\n"
        "    assert True  # reached\n"
        "    assert x == 1\n",
        "tests/unit/test_foo.py",
    )
    assert findings == []


def test_t007_silent_on_assert_false() -> None:
    """assert False is a different smell (always fails) — not T007's target."""
    findings = scan_text(
        "def test_foo():\n    assert False\n", "tests/unit/test_foo.py"
    )
    assert not any(f.rule_id == "T007" for f in findings)


# ── T008: uncollectable test file ────────────────────────────────────────────


def test_t008_fires_on_misnamed_file_with_test_function() -> None:
    findings = scan_text(
        "def test_foo():\n    assert 1 == 1\n", "tests/unit/helpers.py"
    )
    assert [f.rule_id for f in findings] == ["T008"]


def test_t008_fires_on_misnamed_file_with_test_class() -> None:
    findings = scan_text(
        "class TestFoo:\n    def test_bar(self):\n        assert 1 == 1\n",
        "tests/unit/connector_tests.py",
    )
    assert [f.rule_id for f in findings] == ["T008"]


def test_t008_silent_on_correctly_named_file() -> None:
    findings = scan_text(
        "def test_foo():\n    assert 1 == 1\n", "tests/unit/test_foo.py"
    )
    assert not any(f.rule_id == "T008" for f in findings)


def test_t008_silent_on_misnamed_file_with_no_test_shapes() -> None:
    """A helper module with no test*/Test* shapes is not itself a concern."""
    findings = scan_text(
        "def build_fixture():\n    return {}\n", "tests/unit/helpers.py"
    )
    assert findings == []


def test_t008_suppresses_other_findings_for_the_same_file() -> None:
    """Once T008 fires, T005-T007/T009 are not also evaluated for that file."""
    findings = scan_text(
        "def test_foo():\n    pass\n",
        "tests/unit/helpers.py",  # would be T006 if named correctly
    )
    assert [f.rule_id for f in findings] == ["T008"]


# ── T009: unconditional module-level skip ────────────────────────────────────


def test_t009_fires_on_unguarded_module_skip() -> None:
    findings = scan_text(
        "import pytest\n"
        "pytest.skip('disabled', allow_module_level=True)\n\n"
        "def test_foo():\n    assert 1 == 1\n",
        "tests/e2e/test_foo.py",
    )
    assert "T009" in [f.rule_id for f in findings]


def test_t009_silent_on_env_guarded_skip() -> None:
    findings = scan_text(
        "import os\n"
        "import pytest\n"
        "if not os.environ.get('ATLAN_API_KEY'):\n"
        "    pytest.skip('needs env', allow_module_level=True)\n\n"
        "def test_foo():\n    assert 1 == 1\n",
        "tests/e2e/test_foo.py",
    )
    assert not any(f.rule_id == "T009" for f in findings)


def test_t009_silent_on_skip_without_allow_module_level() -> None:
    """A per-test pytest.skip() call (not module-level) is not T009's concern."""
    findings = scan_text(
        "import pytest\n\n" "def test_foo():\n" "    pytest.skip('flaky')\n",
        "tests/unit/test_foo.py",
    )
    assert not any(f.rule_id == "T009" for f in findings)


def test_t009_suppressed_with_directive() -> None:
    findings = scan_text(
        "import pytest\n"
        "pytest.skip(  # conformance: ignore[T009] pending removal in BLDX-9999\n"
        "    'disabled', allow_module_level=True\n"
        ")\n\n"
        "def test_foo():\n    assert 1 == 1\n",
        "tests/e2e/test_foo.py",
    )
    t009 = [f for f in findings if f.rule_id == "T009"]
    assert len(t009) == 1
    assert t009[0].suppressed is True
