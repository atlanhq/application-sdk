"""F016 reads scenario registrations statically; each shape is graded here.

A scenario is defined when a pytest-collected test under ``tests/unit/`` carries
a resolvable ``preflight_conformance`` marker, runs, and reachably calls the
contract assertion it needs from ``conformance.preflight_testing``. Every case
below pairs a defining shape with the nearest shape that must not count, so a
reader that accepts too much or too little fails here rather than on the fleet
dashboard.
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

NOT_RUN = "does not run"
NOT_REGISTERED = "healthy for entrypoint default is not registered"


@pytest.fixture(autouse=True)
def _one_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    """Narrow the matrix so each fixture defines two scenarios, not thirteen."""
    monkeypatch.setitem(SCENARIOS, "F016", ("healthy", "hung_probe"))


def _grade(
    tmp_path: Path, tests: dict[str, str], app: str = "", handler: str = HANDLER
) -> list[str]:
    for rel, body in tests.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "handler.py").write_text(handler)
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


def _module(*tests: str, prelude: str = PRELUDE) -> dict[str, str]:
    return {"tests/unit/test_preflight.py": prelude + "\n\n".join(tests)}


def test_a_complete_literal_matrix_is_defined(tmp_path: Path) -> None:
    assert _grade(tmp_path, _module(_test(HEALTHY), LIFETIME)) == []


def test_a_missing_scenario_is_reported_with_the_marker_to_add(tmp_path: Path) -> None:
    messages = _grade(tmp_path, _module(LIFETIME))
    assert len(messages) == 1
    assert NOT_REGISTERED in messages[0]
    assert 'scenario="healthy")' in messages[0]


# --- whether the test runs ----------------------------------------------------


@pytest.mark.parametrize(
    "decorators",
    [
        '@pytest.mark.skip(reason="later")\n' + HEALTHY,
        '@pytest.mark.skipif(True, reason="later")\n' + HEALTHY,
        '@pytest.mark.skipif(condition=1, reason="later")\n' + HEALTHY,
        "@pytest.mark.xfail\n" + HEALTHY,
        '@pytest.mark.xfail(reason="flaky")\n' + HEALTHY,
    ],
    ids=["skip", "skipif-true", "skipif-kw", "xfail", "xfail-reason"],
)
def test_a_skipped_test_does_not_define_its_scenario(
    tmp_path: Path, decorators: str
) -> None:
    messages = _grade(tmp_path, _module(_test(decorators), LIFETIME))
    assert any(NOT_RUN in m for m in messages)
    assert any(NOT_REGISTERED in m for m in messages)


@pytest.mark.parametrize(
    "decorators",
    [
        '@pytest.mark.skipif(False, reason="platform")\n' + HEALTHY,
        '@pytest.mark.xfail(False, reason="fixed")\n' + HEALTHY,
    ],
    ids=["skipif-false", "xfail-false"],
)
def test_a_literally_false_condition_still_runs(
    tmp_path: Path, decorators: str
) -> None:
    assert _grade(tmp_path, _module(_test(decorators), LIFETIME)) == []


@pytest.mark.parametrize(
    "decorators",
    [
        '@pytest.mark.skipif(sys.platform == "win32", reason="posix")\n' + HEALTHY,
        '@pytest.mark.skipif("sys.platform == \'win32\'", reason="posix")\n' + HEALTHY,
        "@pytest.mark.xfail(condition=FLAKY)\n" + HEALTHY,
    ],
    ids=["expression", "string-condition", "name"],
)
def test_a_dynamic_condition_is_unresolved_not_skipped(
    tmp_path: Path, decorators: str
) -> None:
    messages = _grade(tmp_path, _module(_test(decorators), LIFETIME))
    assert any("cannot be read statically" in m for m in messages)
    assert not any(NOT_RUN in m for m in messages)
    assert any(NOT_REGISTERED in m for m in messages)


@pytest.mark.parametrize(
    "binding",
    [
        'pytestmark = pytest.mark.skip(reason="later")',
        'pytestmark = [pytest.mark.asyncio, pytest.mark.skip(reason="later")]',
        'pytestmark: object = pytest.mark.skip(reason="later")',
        'marks = pytestmark = pytest.mark.skip(reason="later")',
        'pytestmark = []\npytestmark += [pytest.mark.skip(reason="later")]',
    ],
    ids=["plain", "list", "annotated", "chained", "augmented"],
)
def test_a_module_pytestmark_skip_reaches_every_scenario(
    tmp_path: Path, binding: str
) -> None:
    messages = _grade(tmp_path, _module(binding, _test(HEALTHY), LIFETIME))
    assert sum(NOT_RUN in m for m in messages) == 2


def test_an_unreadable_pytestmark_element_keeps_its_readable_skip(
    tmp_path: Path,
) -> None:
    binding = 'pytestmark = [pytest.mark.skip(reason="later"), dynamic_mark()]'
    messages = _grade(tmp_path, _module(binding, _test(HEALTHY), LIFETIME))
    assert sum(NOT_RUN in m for m in messages) == 2


def test_an_unreadable_pytestmark_counts_nothing(tmp_path: Path) -> None:
    messages = _grade(
        tmp_path, _module("pytestmark = marks_for_ci()", _test(HEALTHY), LIFETIME)
    )
    assert sum("cannot be read statically" in m for m in messages) == 2
    assert sum("is not registered" in m for m in messages) == 2


def test_a_skipped_class_reaches_its_methods(tmp_path: Path) -> None:
    body = PRELUDE + (
        '@pytest.mark.skip(reason="later")\n'
        "@some_unrelated_decorator\n"
        "class TestScenarios:\n"
        f"    {HEALTHY}\n"
        "    def test_healthy(self):\n"
        "        assert_preflight_result(result, required_checks=set(), observed_checks=set(), expected_status='ready')\n"
    )
    messages = _grade(
        tmp_path, {"tests/unit/test_preflight.py": body + "\n\n" + LIFETIME}
    )
    assert any(NOT_RUN in m for m in messages)


def test_an_unconditional_runtime_skip_does_not_define_its_scenario(
    tmp_path: Path,
) -> None:
    skipped = _test(HEALTHY, '    pytest.skip("later")\n' + ASSERT)
    assert any(NOT_RUN in m for m in _grade(tmp_path, _module(skipped, LIFETIME)))


def test_a_conditional_runtime_skip_still_defines_it(tmp_path: Path) -> None:
    guarded = _test(
        HEALTHY, '    if windows():\n        pytest.skip("posix only")\n' + ASSERT
    )
    assert _grade(tmp_path, _module(guarded, LIFETIME)) == []


def test_a_declared_unsupported_scenario_is_still_a_gap(tmp_path: Path) -> None:
    unsupported = _test(
        '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy", unsupported=True, reason="n/a")'
    )
    messages = _grade(tmp_path, _module(unsupported, LIFETIME))
    assert any("declared unsupported" in m for m in messages)


# --- the contract assertion ---------------------------------------------------


def test_registration_without_the_contract_assertion_is_not_a_definition(
    tmp_path: Path,
) -> None:
    bare = _test(HEALTHY, "    assert True\n")
    messages = _grade(tmp_path, _module(bare, LIFETIME))
    assert any("never reachably calls assert_preflight_result" in m for m in messages)


def test_the_assertion_may_sit_in_a_module_helper(tmp_path: Path) -> None:
    helper = "def _check(result):\n" + ASSERT
    via_helper = _test(HEALTHY, "    _check(run())\n")
    assert _grade(tmp_path, _module(helper, via_helper, LIFETIME)) == []


@pytest.mark.parametrize(
    "body",
    [
        "    def _never_called():\n    " + ASSERT,
        "    if False:\n    " + ASSERT,
        "    while 0:\n    " + ASSERT,
        "    return\n" + ASSERT,
        "    check = lambda: assert_preflight_result(result, required_checks=set(), observed_checks=set(), expected_status='ready')\n",
    ],
    ids=["nested-def", "if-false", "while-false", "after-return", "lambda"],
)
def test_an_unreachable_assertion_is_not_a_definition(
    tmp_path: Path, body: str
) -> None:
    messages = _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME))
    assert any("never reachably calls assert_preflight_result" in m for m in messages)


def test_an_assertion_in_the_live_branch_counts(tmp_path: Path) -> None:
    body = "    if True:\n    " + ASSERT + "    else:\n        pass\n"
    assert _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME)) == []


@pytest.mark.parametrize(
    ("prelude", "extra"),
    [
        (
            "import pytest\nfrom conformance.preflight_testing import assert_probe_lifetime\n\n",
            "def assert_preflight_result(*args, **kwargs):\n    pass\n",
        ),
        (
            "import pytest\nfrom mylib import assert_preflight_result, assert_probe_lifetime\n\n",
            "",
        ),
        (
            PRELUDE,
            "def assert_preflight_result(*args, **kwargs):\n    pass\n",
        ),
    ],
    ids=["local-definition", "other-module", "import-then-shadow"],
)
def test_a_same_named_function_is_not_the_contract_assertion(
    tmp_path: Path, prelude: str, extra: str
) -> None:
    messages = _grade(
        tmp_path, _module(extra, _test(HEALTHY), LIFETIME, prelude=prelude)
    )
    assert any("never reachably calls assert_preflight_result" in m for m in messages)


def test_a_fixture_parameter_shadows_the_assertion(tmp_path: Path) -> None:
    shadowed = f"{HEALTHY}\ndef test_healthy(assert_preflight_result):\n{ASSERT}"
    messages = _grade(tmp_path, _module(shadowed, LIFETIME))
    assert any("never reachably calls assert_preflight_result" in m for m in messages)


@pytest.mark.parametrize(
    "prelude",
    [
        "import pytest\nimport conformance.preflight_testing as pt\n\n",
        "import pytest\nfrom conformance import preflight_testing as pt\n\n",
        "import pytest\nfrom conformance.preflight_testing import assert_preflight_result as check, assert_probe_lifetime as lifetime\n\n",
    ],
    ids=["module-alias", "from-package", "aliased-names"],
)
def test_the_assertion_resolves_through_aliases(tmp_path: Path, prelude: str) -> None:
    if "as check" in prelude:
        call, lifetime = "check", "lifetime"
    else:
        call, lifetime = "pt.assert_preflight_result", "pt.assert_probe_lifetime"
    healthy = _test(HEALTHY, ASSERT.replace("assert_preflight_result", call))
    hung = LIFETIME.replace("assert_preflight_result", call).replace(
        "assert_probe_lifetime", lifetime
    )
    assert _grade(tmp_path, _module(healthy, hung, prelude=prelude)) == []


def test_a_lifetime_scenario_needs_the_lifetime_assertion(tmp_path: Path) -> None:
    hung = _test(
        '@pytest.mark.preflight_conformance(rule="F016", scenario="hung_probe")',
        name="test_hung",
    )
    messages = _grade(tmp_path, _module(_test(HEALTHY), hung))
    assert any("never reachably calls assert_probe_lifetime" in m for m in messages)


# --- parametrize --------------------------------------------------------------

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

LIFETIME_CALL = (
    "    assert_probe_lifetime(elapsed=0, budget=1, background_stopped=True)\n"
)


def _matrix_test(scenario: str, name: str, extra: str = "") -> str:
    return (
        f'@entrypoint_matrix("{scenario}")\ndef {name}(entrypoint):\n' + ASSERT + extra
    )


def _matrix_module(helper: str = MATRIX_HELPER) -> dict[str, str]:
    return _module(
        helper,
        _matrix_test("healthy", "test_healthy"),
        _matrix_test("hung_probe", "test_hung", LIFETIME_CALL),
    )


def test_the_parametrize_helper_shape_defines_one_scenario_per_entrypoint(
    tmp_path: Path,
) -> None:
    assert _grade(tmp_path, _matrix_module(), app=CONNECTOR) == []


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
    messages = _grade(tmp_path, _matrix_module(helper), app=CONNECTOR)
    assert sum(NOT_RUN in m for m in messages) == 4


def test_a_case_defines_only_the_entrypoint_it_runs(tmp_path: Path) -> None:
    """A marker claiming extract_metadata on a case that runs extract_lineage."""
    swapped = """
def entrypoint_matrix(scenario: str):
    return pytest.mark.parametrize(
        "entrypoint",
        [
            pytest.param("extract_lineage", marks=pytest.mark.preflight_conformance(rule="F016", scenario=scenario, entrypoint="extract_metadata")),
            pytest.param("extract_lineage", marks=pytest.mark.preflight_conformance(rule="F016", scenario=scenario, entrypoint="extract_lineage")),
        ],
    )
"""
    messages = _grade(tmp_path, _matrix_module(swapped), app=CONNECTOR)
    assert sum("run with entrypoint='extract_lineage'" in m for m in messages) == 2
    assert (
        sum("for entrypoint extract_metadata is not registered" in m for m in messages)
        == 2
    )


def test_an_unreadable_case_entrypoint_is_not_counted(tmp_path: Path) -> None:
    dynamic = """
def entrypoint_matrix(scenario: str):
    return pytest.mark.parametrize(
        "entrypoint",
        [
            pytest.param(pick("m"), marks=pytest.mark.preflight_conformance(rule="F016", scenario=scenario, entrypoint="extract_metadata")),
            pytest.param("extract_lineage", marks=pytest.mark.preflight_conformance(rule="F016", scenario=scenario, entrypoint="extract_lineage")),
        ],
    )
"""
    messages = _grade(tmp_path, _matrix_module(dynamic), app=CONNECTOR)
    assert sum("cannot be read statically" in m for m in messages) == 2


@pytest.mark.parametrize(
    "parametrize",
    [
        '@pytest.mark.parametrize("value", [])',
        '@pytest.mark.parametrize("value", [pytest.param(1, marks=pytest.mark.skip(reason="x"))])',
    ],
    ids=["empty", "every-case-skipped"],
)
def test_a_function_marker_needs_a_runnable_case(
    tmp_path: Path, parametrize: str
) -> None:
    decorated = f"{parametrize}\n{HEALTHY}\ndef test_healthy(value):\n{ASSERT}"
    messages = _grade(tmp_path, _module(decorated, LIFETIME))
    assert any(NOT_RUN in m for m in messages)


def test_a_function_marker_with_one_runnable_case_counts(tmp_path: Path) -> None:
    parametrize = '@pytest.mark.parametrize("value", [1, pytest.param(2, marks=pytest.mark.skip(reason="x"))])'
    decorated = f"{parametrize}\n{HEALTHY}\ndef test_healthy(value):\n{ASSERT}"
    assert _grade(tmp_path, _module(decorated, LIFETIME)) == []


# --- what the reader cannot resolve --------------------------------------------


def test_an_unresolvable_scenario_is_reported_not_counted(tmp_path: Path) -> None:
    dynamic = _test('@pytest.mark.preflight_conformance(rule="F016", scenario=pick())')
    messages = _grade(tmp_path, _module(dynamic, LIFETIME))
    assert any("not counted as coverage" in m for m in messages)
    assert any(NOT_REGISTERED in m for m in messages)


def test_an_unresolvable_helper_is_reported_not_counted(tmp_path: Path) -> None:
    helper = (
        "def register(scenario):\n"
        "    marks = build(scenario)\n"
        '    return pytest.mark.preflight_conformance(rule="F016", scenario=marks)\n'
    )
    dynamic = '@register("healthy")\ndef test_healthy():\n' + ASSERT
    messages = _grade(tmp_path, _module(helper, dynamic, LIFETIME))
    assert any("not statically resolvable" in m for m in messages)


# --- where scenarios live -------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "tests/unit/preflight_scenarios.py",
        "app/test_preflight.py",
        "tests/test_preflight.py",
        "tests/integration/test_preflight.py",
        "tests/e2e/test_preflight.py",
    ],
    ids=["not-collected-name", "outside-tests", "tests-root", "integration", "e2e"],
)
def test_only_the_unit_tier_defines_scenarios(tmp_path: Path, rel: str) -> None:
    """The unit job always runs tests/unit/; nothing else is guaranteed to run."""
    messages = _grade(tmp_path, {rel: PRELUDE + _test(HEALTHY) + "\n\n" + LIFETIME})
    assert sum("is not registered" in m for m in messages) == 2


def test_a_nested_unit_module_counts(tmp_path: Path) -> None:
    body = {
        "tests/unit/preflight/test_scenarios.py": PRELUDE
        + _test(HEALTHY)
        + "\n\n"
        + LIFETIME
    }
    assert _grade(tmp_path, body) == []


def test_a_function_pytest_would_not_collect_defines_nothing(tmp_path: Path) -> None:
    helper_named = _test(HEALTHY, name="check_healthy")
    messages = _grade(tmp_path, _module(helper_named, LIFETIME))
    assert any(NOT_REGISTERED in m for m in messages)


def test_an_unknown_scenario_is_reported(tmp_path: Path) -> None:
    typo = _test(
        '@pytest.mark.preflight_conformance(rule="F016", scenario="helthy")',
        name="test_typo",
    )
    messages = _grade(tmp_path, _module(_test(HEALTHY), LIFETIME, typo))
    assert any("helthy" in m and "not in the F016 matrix" in m for m in messages)


def test_a_retired_rule_registration_is_ignored(tmp_path: Path) -> None:
    retired = _test(
        '@pytest.mark.preflight_conformance(rule="F017", scenario="not_ready")',
        name="test_gate",
    )
    assert _grade(tmp_path, _module(_test(HEALTHY), LIFETIME, retired)) == []


def test_from_pytest_import_mark_is_read(tmp_path: Path) -> None:
    prelude = PRELUDE.replace("import pytest\n", "from pytest import mark\n")
    body = _module(
        _test(HEALTHY.replace("@pytest.mark.", "@mark.")),
        LIFETIME.replace("@pytest.mark.", "@mark."),
        prelude=prelude,
    )
    assert _grade(tmp_path, body) == []


# --- which apps owe the matrix ----------------------------------------------------


def test_an_app_without_its_own_preflight_owes_no_scenarios(tmp_path: Path) -> None:
    """The scenarios drive the real handler; inheriting the SDK default leaves none."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "connector.py").write_text(CONNECTOR)
    assert scan_all(discover(tmp_path), tmp_path) == []


def test_an_annotated_preflight_binding_owes_the_matrix(tmp_path: Path) -> None:
    handler = (
        "from collections.abc import Callable\n"
        "from application_sdk.handler import Handler\n"
        "async def check_source(input): ...\n"
        "class H(Handler):\n"
        "    preflight_check: Callable = check_source\n"
    )
    messages = _grade(tmp_path, _module(), handler=handler)
    assert sum("is not registered" in m for m in messages) == 2


def test_a_preflight_check_data_field_is_not_a_hook(tmp_path: Path) -> None:
    """Generated input contracts carry `preflight_check: str = ""` as data."""
    (tmp_path / "app" / "generated").mkdir(parents=True)
    (tmp_path / "app" / "generated" / "_input.py").write_text(
        "from pydantic import BaseModel\n"
        "class ExtractMetadataInput(BaseModel):\n"
        '    preflight_check: str = ""\n'
    )
    assert scan_all(discover(tmp_path), tmp_path) == []


def test_scan_all_keeps_test_modules_out_of_the_app_analysis(tmp_path: Path) -> None:
    """A handler defined in a test module is not the app's handler."""
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_preflight.py").write_text(
        "from application_sdk.handler import Handler\n"
        "class H(Handler):\n"
        "    async def preflight_check(self, input):\n"
        "        return True\n"
    )
    assert scan_all(discover(tmp_path), tmp_path) == []


# --- second review round ------------------------------------------------------

NEVER_CALLS = "never reachably calls assert_preflight_result"
UNREADABLE = "cannot be read statically"


@pytest.mark.parametrize(
    "rebinding",
    [
        "from mylib import assert_preflight_result",
        "try:\n    import fast\nexcept ImportError:\n    assert_preflight_result = print",
        "if True:\n    def assert_preflight_result(*args, **kwargs):\n        pass",
    ],
    ids=["later-import", "fallback-in-try", "definition-in-if"],
)
def test_a_later_rebinding_replaces_the_contract_import(
    tmp_path: Path, rebinding: str
) -> None:
    messages = _grade(tmp_path, _module(rebinding, _test(HEALTHY), LIFETIME))
    assert any(NEVER_CALLS in m for m in messages)


def test_a_contract_import_after_an_unrelated_one_is_credited(tmp_path: Path) -> None:
    prelude = (
        "import pytest\nfrom mylib import assert_preflight_result\n"
        + PRELUDE.replace("import pytest\n", "")
    )
    assert _grade(tmp_path, _module(_test(HEALTHY), LIFETIME, prelude=prelude)) == []


def test_a_shadowed_dotted_root_is_not_the_contract_module(tmp_path: Path) -> None:
    prelude = "import pytest\nimport conformance.preflight_testing\n\n"
    call = "conformance.preflight_testing.assert_preflight_result"
    lifetime = LIFETIME.replace("assert_preflight_result", call).replace(
        "assert_probe_lifetime", "conformance.preflight_testing.assert_probe_lifetime"
    )
    healthy_body = ASSERT.replace("assert_preflight_result", call)
    shadowed = f"{HEALTHY}\ndef test_healthy(conformance):\n{healthy_body}"
    messages = _grade(tmp_path, _module(shadowed, lifetime, prelude=prelude))
    assert any(NEVER_CALLS in m for m in messages)
    # The same call without the shadowing parameter is the contract assertion.
    plain = _test(HEALTHY, healthy_body)
    assert _grade(tmp_path, _module(plain, lifetime, prelude=prelude)) == []


@pytest.mark.parametrize(
    "body",
    [
        "    if True:\n        return\n" + ASSERT,
        "    if ready():\n        return\n    else:\n        return\n" + ASSERT,
        "    with context():\n        return\n" + ASSERT,
        "    assert 0, 'unreachable'\n" + ASSERT,
        "    False and " + ASSERT.lstrip(),
        "    True or " + ASSERT.lstrip(),
        "    checks = (" + ASSERT.strip() + " for _ in range(1))\n",
        "    assert result, " + ASSERT.strip() + "\n",
        "    value = "
        + ASSERT.strip().replace(
            "assert_preflight_result(", "(assert_preflight_result("
        )
        + ") if False else None\n",
    ],
    ids=[
        "literal-branch-return",
        "both-branches-return",
        "return-inside-with",
        "assert-literal-false",
        "and-false",
        "or-true",
        "unconsumed-generator",
        "assert-message",
        "dead-ifexp-branch",
    ],
)
def test_a_statically_skipped_assertion_is_not_credited(
    tmp_path: Path, body: str
) -> None:
    messages = _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME))
    assert any(NEVER_CALLS in m for m in messages)


@pytest.mark.parametrize(
    "body",
    [
        "    if ready():\n        return\n" + ASSERT,
        "    with pytest.raises(ValueError):\n        raise ValueError()\n" + ASSERT,
        "    try:\n        raise ValueError()\n    except ValueError:\n        pass\n"
        + ASSERT,
        "    ready() and " + ASSERT.lstrip(),
        "    checks = [" + ASSERT.strip() + " for _ in range(1)]\n",
        "    for attempt in range(2):\n        if attempt:\n            break\n"
        + ASSERT,
    ],
    ids=[
        "one-branch-returns",
        "raise-absorbed-by-with",
        "raise-caught-by-try",
        "dynamic-and",
        "eager-list-comprehension",
        "break-ends-only-the-loop",
    ],
)
def test_an_assertion_that_can_run_is_credited(tmp_path: Path, body: str) -> None:
    assert _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME)) == []


def test_an_unresolved_parametrize_withholds_a_function_marker(tmp_path: Path) -> None:
    decorated = (
        '@pytest.mark.parametrize("entrypoint", build_cases())\n'
        f"{HEALTHY}\ndef test_healthy(entrypoint):\n{ASSERT}"
    )
    messages = _grade(tmp_path, _module(decorated, LIFETIME))
    assert any(UNREADABLE in m for m in messages)
    assert any(NOT_REGISTERED in m for m in messages)


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ('skip_ci = pytest.mark.skipif(True, reason="ci")', NOT_RUN),
        ('skip_ci = pytest.mark.skipif(os.environ.get("CI"), reason="ci")', UNREADABLE),
        ("skip_ci = pytest.mark.skipif(*conditions())", UNREADABLE),
    ],
    ids=["literal", "dynamic-condition", "unreadable-alias"],
)
def test_a_module_mark_alias_is_applied(
    tmp_path: Path, alias: str, expected: str
) -> None:
    decorated = _test("@skip_ci\n" + HEALTHY)
    messages = _grade(tmp_path, _module(alias, decorated, LIFETIME))
    assert any(expected in m for m in messages)


def test_a_decorator_that_applies_no_marks_is_ignored(tmp_path: Path) -> None:
    decorated = _test("@respx.mock\n@pytest.mark.asyncio\n" + HEALTHY)
    assert _grade(tmp_path, _module("import respx", decorated, LIFETIME)) == []


def _entrypoint_param(values: list[str]) -> str:
    cases = ", ".join(values)
    return f'@pytest.mark.parametrize("entrypoint", [{cases}])'


LINEAGE_ONLY = '@pytest.mark.parametrize("entrypoint", ["extract_lineage"])'


def test_a_function_marker_needs_a_case_with_its_entrypoint(tmp_path: Path) -> None:
    marker = '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy", entrypoint="extract_metadata")'
    decorated = f"{LINEAGE_ONLY}\n{marker}\ndef test_healthy(entrypoint):\n{ASSERT}"
    messages = _grade(tmp_path, _module(decorated, LIFETIME), app=CONNECTOR)
    assert any("run with entrypoint='extract_lineage'" in m for m in messages)
    assert any(
        "healthy for entrypoint extract_metadata is not registered" in m
        for m in messages
    )


def test_a_function_marker_with_a_matching_runnable_case_counts(tmp_path: Path) -> None:
    marker = '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy", entrypoint="extract_metadata")'
    cases = ['"extract_lineage"', '"extract_metadata"']
    decorated = (
        f"{_entrypoint_param(cases)}\n{marker}\ndef test_healthy(entrypoint):\n{ASSERT}"
    )
    messages = _grade(tmp_path, _module(decorated, LIFETIME), app=CONNECTOR)
    assert not any("healthy for entrypoint extract_metadata" in m for m in messages)


def test_a_matching_case_that_is_skipped_does_not_count(tmp_path: Path) -> None:
    marker = '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy", entrypoint="extract_metadata")'
    cases = [
        '"extract_lineage"',
        'pytest.param("extract_metadata", marks=pytest.mark.skip(reason="x"))',
    ]
    decorated = (
        f"{_entrypoint_param(cases)}\n{marker}\ndef test_healthy(entrypoint):\n{ASSERT}"
    )
    messages = _grade(tmp_path, _module(decorated, LIFETIME), app=CONNECTOR)
    assert any("run with entrypoint='extract_lineage'" in m for m in messages)


def test_a_stacked_entrypoint_parametrize_is_matched(tmp_path: Path) -> None:
    """The marker rides one parametrize; the entrypoint comes from another."""
    decorated = (
        f"{LINEAGE_ONLY}\n"
        '@pytest.mark.parametrize("mode", [pytest.param("soft", marks=pytest.mark.preflight_conformance(rule="F016", scenario="healthy", entrypoint="extract_metadata"))])\n'
        f"def test_healthy(entrypoint, mode):\n{ASSERT}"
    )
    messages = _grade(tmp_path, _module(decorated, LIFETIME), app=CONNECTOR)
    assert any("run with entrypoint='extract_lineage'" in m for m in messages)


@pytest.mark.parametrize("value", ["None", "0", "('extract_metadata',)"])
def test_a_non_string_case_entrypoint_is_a_mismatch(tmp_path: Path, value: str) -> None:
    helper = f"""
def entrypoint_matrix(scenario: str):
    return pytest.mark.parametrize(
        "entrypoint",
        [
            pytest.param({value}, marks=pytest.mark.preflight_conformance(rule="F016", scenario=scenario, entrypoint="extract_metadata")),
            pytest.param("extract_lineage", marks=pytest.mark.preflight_conformance(rule="F016", scenario=scenario, entrypoint="extract_lineage")),
        ],
    )
"""
    messages = _grade(tmp_path, _matrix_module(helper), app=CONNECTOR)
    assert (
        sum("but its runnable cases run with entrypoint=" in m for m in messages) == 2
    )


# --- third review round -------------------------------------------------------


@pytest.mark.parametrize(
    "dead",
    [
        "if False:\n    assert_preflight_result = print",
        "while False:\n    assert_preflight_result = print",
        "if True:\n    pass\nelse:\n    from mylib import assert_preflight_result",
    ],
    ids=["if-false", "while-false", "dead-else"],
)
def test_a_dead_branch_rebinding_keeps_the_contract_import(
    tmp_path: Path, dead: str
) -> None:
    assert _grade(tmp_path, _module(dead, _test(HEALTHY), LIFETIME)) == []


def test_a_live_branch_rebinding_still_replaces_it(tmp_path: Path) -> None:
    live = "if True:\n    assert_preflight_result = print"
    messages = _grade(tmp_path, _module(live, _test(HEALTHY), LIFETIME))
    assert any(NEVER_CALLS in m for m in messages)


def test_a_later_def_replaces_a_mark_alias(tmp_path: Path) -> None:
    rebinding = (
        'skip_ci = pytest.mark.skipif(False, reason="x")\n\n\n'
        "def skip_ci(fn):\n"
        '    return pytest.mark.skip(reason="x")(fn)'
    )
    decorated = _test("@skip_ci\n" + HEALTHY)
    messages = _grade(tmp_path, _module(rebinding, decorated, LIFETIME))
    assert any(UNREADABLE in m for m in messages)
    assert any(NOT_REGISTERED in m for m in messages)


def test_a_later_import_replaces_a_mark_alias(tmp_path: Path) -> None:
    rebinding = (
        'skip_ci = pytest.mark.skipif(True, reason="x")\nfrom helpers import skip_ci'
    )
    decorated = _test("@skip_ci\n" + HEALTHY)
    # The imported decorator is opaque and applies no mark the reader can see.
    assert _grade(tmp_path, _module(rebinding, decorated, LIFETIME)) == []


def test_an_alias_defined_after_a_def_is_the_alias(tmp_path: Path) -> None:
    rebinding = (
        "def skip_ci(fn):\n    return fn\n\n\n"
        'skip_ci = pytest.mark.skipif(True, reason="x")'
    )
    decorated = _test("@skip_ci\n" + HEALTHY)
    messages = _grade(tmp_path, _module(rebinding, decorated, LIFETIME))
    assert any(NOT_RUN in m for m in messages)


@pytest.mark.parametrize(
    "factory",
    [
        "def identity():\n    return lambda fn: fn",
        "def identity(fn=None):\n    def wrap(f):\n        return f\n    return wrap",
    ],
    ids=["lambda", "closure"],
)
def test_an_ordinary_decorator_factory_is_ignored(tmp_path: Path, factory: str) -> None:
    decorated = _test("@identity()\n" + HEALTHY)
    assert _grade(tmp_path, _module(factory, decorated, LIFETIME)) == []


def test_a_bare_helper_decorator_that_builds_a_mark_is_unknown(tmp_path: Path) -> None:
    helper = "def gate(fn):\n    return pytest.mark.skipif(flaky(), reason='x')(fn)"
    decorated = _test("@gate\n" + HEALTHY)
    messages = _grade(tmp_path, _module(helper, decorated, LIFETIME))
    assert any(UNREADABLE in m for m in messages)


def test_a_deferred_mark_producing_decorator_is_unknown(tmp_path: Path) -> None:
    helper = (
        "def gate():\n" "    return lambda fn: pytest.mark.skip(reason='later')(fn)"
    )
    decorated = _test("@gate()\n" + HEALTHY)
    messages = _grade(tmp_path, _module(helper, decorated, LIFETIME))
    assert any(UNREADABLE in m for m in messages)
    assert any(NOT_REGISTERED in m for m in messages)


@pytest.mark.parametrize(
    "body",
    [
        "    if ready():\n        return\n    else:\n        raise RuntimeError()\n"
        + ASSERT,
        "    if ready():\n        raise RuntimeError()\n    return\n" + ASSERT,
        "    while True:\n        if ready():\n            return\n        raise RuntimeError()\n"
        + ASSERT,
        "    try:\n        return\n    finally:\n        raise RuntimeError()\n"
        + ASSERT,
    ],
    ids=["return-or-raise", "raise-then-return", "infinite-loop", "finally-raises"],
)
def test_a_mixed_definite_exit_ends_the_test(tmp_path: Path, body: str) -> None:
    messages = _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME))
    assert any(NEVER_CALLS in m for m in messages)


@pytest.mark.parametrize(
    "body",
    [
        "    with pytest.raises(RuntimeError):\n        if ready():\n            return\n        raise RuntimeError()\n"
        + ASSERT,
        "    try:\n        if ready():\n            return\n        raise RuntimeError()\n    except RuntimeError:\n        pass\n"
        + ASSERT,
        "    while True:\n        if ready():\n            break\n        raise RuntimeError()\n"
        + ASSERT,
    ],
    ids=["raise-absorbed-by-with", "raise-caught-by-try", "break-leaves-loop"],
)
def test_a_mixed_exit_that_can_be_absorbed_still_reaches_the_assertion(
    tmp_path: Path, body: str
) -> None:
    assert _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME)) == []


# --- fourth review round ------------------------------------------------------


def test_a_statically_empty_loop_preserves_a_mark_alias(tmp_path: Path) -> None:
    alias = (
        'gate = pytest.mark.skip(reason="skip")\n' "for _ in []:\n    gate = object()"
    )
    messages = _grade(tmp_path, _module(alias, _test("@gate\n" + HEALTHY), LIFETIME))
    assert any(NOT_RUN in message for message in messages)


def test_an_unknown_loop_binding_makes_the_alias_unresolved(tmp_path: Path) -> None:
    alias = (
        'gate = pytest.mark.skipif(False, reason="skip")\n'
        "for _ in dynamic_values():\n    gate = object()"
    )
    messages = _grade(tmp_path, _module(alias, _test("@gate\n" + HEALTHY), LIFETIME))
    assert any(UNREADABLE in message for message in messages)
    assert any(NOT_REGISTERED in message for message in messages)


def test_a_decorator_uses_bindings_at_its_definition_site(tmp_path: Path) -> None:
    source = (
        'gate = pytest.mark.skip(reason="skip")\n'
        + _test("@gate\n" + HEALTHY)
        + "\ngate = object()"
    )
    messages = _grade(tmp_path, _module(source, LIFETIME))
    assert any(NOT_RUN in message for message in messages)
    assert any(NOT_REGISTERED in message for message in messages)


def test_a_copied_mark_alias_keeps_its_original_value(tmp_path: Path) -> None:
    source = (
        'gate = pytest.mark.skipif(False, reason="source")\n'
        "saved = gate\n"
        'gate = pytest.mark.skipif(True, reason="rebound")\n'
        + _test("@saved\n" + HEALTHY)
    )
    assert _grade(tmp_path, _module(source, LIFETIME)) == []


def test_a_transitive_local_decorator_that_may_skip_is_unknown(tmp_path: Path) -> None:
    helpers = (
        "def inner():\n    return pytest.mark.skip(reason='skip')\n\n"
        "def outer(fn):\n"
        "    if dynamic():\n        return inner()(fn)\n"
        "    return fn"
    )
    messages = _grade(
        tmp_path,
        _module(helpers, _test("@outer()\n" + HEALTHY), LIFETIME),
    )
    assert any(UNREADABLE in message for message in messages)
    assert any(NOT_REGISTERED in message for message in messages)


def test_a_dead_mark_in_an_identity_decorator_is_ignored(tmp_path: Path) -> None:
    helper = (
        "def identity(fn):\n"
        "    if False:\n        return pytest.mark.skip(fn)\n"
        "    return fn"
    )
    assert (
        _grade(tmp_path, _module(helper, _test("@identity\n" + HEALTHY), LIFETIME))
        == []
    )


def test_an_unused_mark_lambda_in_an_identity_decorator_is_ignored(
    tmp_path: Path,
) -> None:
    helper = (
        "def identity(fn):\n"
        "    unused = lambda: pytest.mark.skip(fn)\n"
        "    return fn"
    )
    assert (
        _grade(tmp_path, _module(helper, _test("@identity\n" + HEALTHY), LIFETIME))
        == []
    )


def test_a_return_only_try_does_not_make_its_handler_reachable(tmp_path: Path) -> None:
    body = "    try:\n        return\n    except Exception:\n        pass\n" + ASSERT
    messages = _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME))
    assert any(NEVER_CALLS in message for message in messages)


def test_a_caught_call_can_reach_the_contract_assertion(tmp_path: Path) -> None:
    body = (
        "    try:\n"
        "        check_source()\n"
        "    except ExpectedError:\n"
        "        " + ASSERT.lstrip()
    )
    assert _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME)) == []


def test_an_inner_handler_does_not_make_an_outer_handler_reachable(
    tmp_path: Path,
) -> None:
    body = (
        "    try:\n"
        "        try:\n"
        "            check_source()\n"
        "        except ExpectedError:\n"
        "            pass\n"
        "    except Exception:\n"
        "        assert_preflight_result(result, required_checks=set(), "
        "observed_checks=set(), expected_status='ready')\n"
    )
    messages = _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME))
    assert any(NEVER_CALLS in message for message in messages)


def test_a_mixed_finally_exit_can_be_absorbed_and_reach_the_assertion(
    tmp_path: Path,
) -> None:
    body = (
        "    with pytest.raises(RuntimeError):\n"
        "        try:\n            pass\n"
        "        finally:\n            if dynamic():\n"
        "                raise RuntimeError()\n" + ASSERT
    )
    assert _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME)) == []
