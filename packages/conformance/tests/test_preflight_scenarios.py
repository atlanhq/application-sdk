"""F016 reads scenario registrations statically; each shape is graded here.

A scenario is defined when a pytest-collected test under ``tests/`` carries a
resolvable ``preflight_conformance`` marker, is not skipped, and calls the
contract assertion it needs. Every case below pairs a defining shape with the
nearest shape that must not count, so a reader that accepts too much or too
little fails here rather than on the fleet dashboard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.preflight_scenarios import SCENARIOS
from conformance.suite.checks.preflight import discover, scan_all
from conformance.suite.checks.preflight._common import build_registry
from conformance.suite.checks.preflight._scenarios import scan

HEALTHY = '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy")'
ASSERT = "    assert_preflight_result(result, required_checks=set(), observed_checks=set(), expected_status='ready')\n"
PRELUDE = "import pytest\nfrom conformance.preflight_testing import assert_preflight_result, assert_probe_lifetime\n\n"

HANDLER = (
    "from application_sdk.handler import Handler\n"
    "class H(Handler):\n"
    "    async def preflight_check(self, input): ...\n"
)

CONNECTOR = (
    "from application_sdk.app import App, entrypoint\n"
    "class C(App):\n"
    "    @entrypoint\n"
    "    async def extract_metadata(self, input): pass\n"
    "    @entrypoint\n"
    "    async def extract_lineage(self, input): pass\n"
)


@pytest.fixture(autouse=True)
def _one_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    """Narrow the matrix so each fixture defines one scenario, not thirteen."""
    monkeypatch.setitem(SCENARIOS, "F016", ("healthy", "hung_probe"))


def _grade(tmp_path: Path, tests: dict[str, str], app: str = "") -> list[str]:
    for rel, body in tests.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "handler.py").write_text(HANDLER)
    if app:
        (tmp_path / "app" / "connector.py").write_text(app)
    paths = discover(tmp_path)
    test_paths = [p for p in paths if p.relative_to(tmp_path).parts[0] == "tests"]
    sources = [p for p in paths if p not in test_paths]
    findings = scan(
        build_registry(sources, tmp_path), build_registry(test_paths, tmp_path)
    )
    return [f.message for f in findings if not f.suppressed]


def _test(decorators: str, body: str = ASSERT, name: str = "test_healthy") -> str:
    return f"{decorators}\ndef {name}():\n{body}"


LIFETIME = _test(
    '@pytest.mark.preflight_conformance(rule="F016", scenario="hung_probe")',
    ASSERT
    + "    assert_probe_lifetime(elapsed=0, budget=1, background_stopped=True)\n",
    "test_hung",
)


def _module(*tests: str) -> dict[str, str]:
    return {"tests/unit/test_preflight.py": PRELUDE + "\n\n".join(tests)}


def test_a_complete_literal_matrix_is_defined(tmp_path: Path) -> None:
    assert _grade(tmp_path, _module(_test(HEALTHY), LIFETIME)) == []


def test_a_missing_scenario_is_reported_with_the_marker_to_add(tmp_path: Path) -> None:
    messages = _grade(tmp_path, _module(LIFETIME))
    assert len(messages) == 1
    assert "healthy for entrypoint default is not registered" in messages[0]
    assert 'scenario="healthy")' in messages[0]


@pytest.mark.parametrize(
    "decorators",
    [
        '@pytest.mark.skip(reason="later")\n' + HEALTHY,
        '@pytest.mark.skipif(True, reason="later")\n' + HEALTHY,
        "@pytest.mark.xfail\n" + HEALTHY,
    ],
    ids=["skip", "skipif", "xfail"],
)
def test_a_skipped_test_does_not_define_its_scenario(
    tmp_path: Path, decorators: str
) -> None:
    messages = _grade(tmp_path, _module(_test(decorators), LIFETIME))
    assert any("skipped or xfail" in m for m in messages)
    assert any(
        "healthy for entrypoint default is not registered" in m for m in messages
    )


def test_a_module_pytestmark_skip_reaches_every_scenario(tmp_path: Path) -> None:
    body = _module(
        'pytestmark = pytest.mark.skip(reason="later")', _test(HEALTHY), LIFETIME
    )
    messages = _grade(tmp_path, body)
    assert sum("skipped or xfail" in m for m in messages) == 2


def test_a_skipped_class_reaches_its_methods(tmp_path: Path) -> None:
    body = PRELUDE + (
        '@pytest.mark.skip(reason="later")\n'
        "class TestScenarios:\n"
        f"    {HEALTHY}\n"
        "    def test_healthy(self):\n"
        "        assert_preflight_result(result, required_checks=set(), observed_checks=set(), expected_status='ready')\n"
    )
    messages = _grade(tmp_path, {"tests/test_preflight.py": body + "\n\n" + LIFETIME})
    assert any("skipped or xfail" in m for m in messages)


def test_an_unconditional_runtime_skip_does_not_define_its_scenario(
    tmp_path: Path,
) -> None:
    skipped = _test(HEALTHY, '    pytest.skip("later")\n' + ASSERT)
    assert any(
        "skipped or xfail" in m for m in _grade(tmp_path, _module(skipped, LIFETIME))
    )


def test_a_conditional_runtime_skip_still_defines_it(tmp_path: Path) -> None:
    guarded = _test(
        HEALTHY, '    if windows():\n        pytest.skip("posix only")\n' + ASSERT
    )
    assert _grade(tmp_path, _module(guarded, LIFETIME)) == []


def test_a_declared_unsupported_scenario_is_still_a_gap(tmp_path: Path) -> None:
    unsupported = _test(
        '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy", unsupported=True, reason="n/a")'
    )
    assert any(
        "declared unsupported" in m
        for m in _grade(tmp_path, _module(unsupported, LIFETIME))
    )


def test_registration_without_the_contract_assertion_is_not_a_definition(
    tmp_path: Path,
) -> None:
    bare = _test(HEALTHY, "    assert True\n")
    messages = _grade(tmp_path, _module(bare, LIFETIME))
    assert any("never calls assert_preflight_result" in m for m in messages)


def test_the_assertion_may_sit_in_a_module_helper(tmp_path: Path) -> None:
    helper = "def _check(result):\n" + ASSERT
    via_helper = _test(HEALTHY, "    _check(run())\n")
    assert _grade(tmp_path, _module(helper, via_helper, LIFETIME)) == []


def test_a_lifetime_scenario_needs_the_lifetime_assertion(tmp_path: Path) -> None:
    hung = _test(
        '@pytest.mark.preflight_conformance(rule="F016", scenario="hung_probe")',
        name="test_hung",
    )
    messages = _grade(tmp_path, _module(_test(HEALTHY), hung))
    assert any("never calls assert_probe_lifetime" in m for m in messages)


MATRIX_HELPER = '''
ENTRYPOINTS = ("extract_metadata", "extract_lineage")


def entrypoint_matrix(scenario: str):
    """One registration per entrypoint, as atlan-metabase-app does it."""
    return pytest.mark.parametrize(
        "entrypoint",
        [
            pytest.param(
                name,
                id=name,
                marks=pytest.mark.preflight_conformance(
                    rule="F016", scenario=scenario, entrypoint=name
                ),
            )
            for name in ENTRYPOINTS
        ],
    )
'''


def _matrix_test(scenario: str, name: str, extra: str = "") -> str:
    return (
        f'@entrypoint_matrix("{scenario}")\n'
        f"def {name}(entrypoint):\n" + ASSERT + extra
    )


LIFETIME_CALL = (
    "    assert_probe_lifetime(elapsed=0, budget=1, background_stopped=True)\n"
)


def test_the_parametrize_helper_shape_defines_one_scenario_per_entrypoint(
    tmp_path: Path,
) -> None:
    body = _module(
        MATRIX_HELPER,
        _matrix_test("healthy", "test_healthy"),
        _matrix_test("hung_probe", "test_hung", LIFETIME_CALL),
    )
    assert _grade(tmp_path, body, app=CONNECTOR) == []


def test_the_matrix_is_owed_per_declared_entrypoint(tmp_path: Path) -> None:
    """A function-level marker registers `default` only; both entrypoints are owed."""
    messages = _grade(tmp_path, _module(_test(HEALTHY), LIFETIME), app=CONNECTOR)
    assert any(
        "entrypoint default, which is not in the F016 matrix" in m for m in messages
    )
    assert sum("is not registered" in m for m in messages) == 4


def test_a_param_level_skip_removes_only_that_entrypoint(tmp_path: Path) -> None:
    helper = MATRIX_HELPER.replace(
        "                marks=pytest.mark.preflight_conformance(\n"
        '                    rule="F016", scenario=scenario, entrypoint=name\n'
        "                ),\n",
        "                marks=[\n"
        "                    pytest.mark.preflight_conformance(\n"
        '                        rule="F016", scenario=scenario, entrypoint=name\n'
        "                    ),\n"
        '                    pytest.mark.skip(reason="later"),\n'
        "                ],\n",
    )
    body = _module(
        helper,
        _matrix_test("healthy", "test_healthy"),
        _matrix_test("hung_probe", "test_hung", LIFETIME_CALL),
    )
    messages = _grade(tmp_path, body, app=CONNECTOR)
    assert sum("skipped or xfail" in m for m in messages) == 4


def test_an_unresolvable_scenario_is_reported_not_counted(tmp_path: Path) -> None:
    dynamic = _test('@pytest.mark.preflight_conformance(rule="F016", scenario=pick())')
    messages = _grade(tmp_path, _module(dynamic, LIFETIME))
    assert any("not statically resolvable" in m for m in messages)
    assert any(
        "healthy for entrypoint default is not registered" in m for m in messages
    )


def test_an_unresolvable_helper_is_reported_not_counted(tmp_path: Path) -> None:
    helper = (
        "def register(scenario):\n"
        "    marks = build(scenario)\n"
        '    return pytest.mark.preflight_conformance(rule="F016", scenario=marks)\n'
    )
    dynamic = '@register("healthy")\ndef test_healthy():\n' + ASSERT
    messages = _grade(tmp_path, _module(helper, dynamic, LIFETIME))
    assert any("not statically resolvable" in m for m in messages)


@pytest.mark.parametrize(
    "rel",
    ["tests/unit/preflight_scenarios.py", "app/test_preflight.py"],
    ids=["not-collected-name", "outside-tests"],
)
def test_a_module_pytest_would_not_collect_defines_nothing(
    tmp_path: Path, rel: str
) -> None:
    messages = _grade(tmp_path, {rel: PRELUDE + _test(HEALTHY) + "\n\n" + LIFETIME})
    assert sum("is not registered" in m for m in messages) == 2


def test_a_function_pytest_would_not_collect_defines_nothing(tmp_path: Path) -> None:
    helper_named = _test(HEALTHY, name="check_healthy")
    messages = _grade(tmp_path, _module(helper_named, LIFETIME))
    assert any(
        "healthy for entrypoint default is not registered" in m for m in messages
    )


def test_an_unknown_scenario_is_reported(tmp_path: Path) -> None:
    typo = _test('@pytest.mark.preflight_conformance(rule="F016", scenario="helthy")')
    messages = _grade(
        tmp_path,
        _module(_test(HEALTHY), LIFETIME, typo.replace("test_healthy", "test_typo")),
    )
    assert any("helthy" in m and "not in the F016 matrix" in m for m in messages)


def test_a_retired_rule_registration_is_ignored(tmp_path: Path) -> None:
    retired = _test(
        '@pytest.mark.preflight_conformance(rule="F017", scenario="not_ready")',
        name="test_gate",
    )
    assert _grade(tmp_path, _module(_test(HEALTHY), LIFETIME, retired)) == []


def test_from_pytest_import_mark_is_read(tmp_path: Path) -> None:
    body = {
        "tests/test_preflight.py": PRELUDE.replace(
            "import pytest\n", "from pytest import mark\n"
        )
        + _test(HEALTHY.replace("@pytest.mark.", "@mark."))
        + "\n\n"
        + LIFETIME.replace("@pytest.mark.", "@mark.")
    }
    assert _grade(tmp_path, body) == []


def test_an_app_without_its_own_preflight_owes_no_scenarios(tmp_path: Path) -> None:
    """The scenarios drive the real handler; inheriting the SDK default leaves none."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "connector.py").write_text(CONNECTOR)
    assert scan_all(discover(tmp_path), tmp_path) == []


def test_scan_all_keeps_test_modules_out_of_the_app_analysis(tmp_path: Path) -> None:
    """A handler defined in a test module is not the app's handler."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_preflight.py").write_text(
        "from application_sdk.handler import Handler\n"
        "class H(Handler):\n"
        "    async def preflight_check(self, input):\n"
        "        return True\n"
    )
    assert scan_all(discover(tmp_path), tmp_path) == []
