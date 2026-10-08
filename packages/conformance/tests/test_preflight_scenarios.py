"""F016 reads scenario registrations statically; each shape is graded here.

A scenario is defined when a pytest-collected test under ``tests/unit/`` carries
a resolvable ``preflight_conformance`` marker, runs, and reachably calls the
contract assertion it needs from ``conformance.preflight_testing``. Every case
below pairs a defining shape with the nearest shape that must not count, so a
reader that accepts too much or too little fails here rather than on the fleet
dashboard.
"""

from __future__ import annotations

import warnings
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
    assert any("does not call assert_preflight_result" in m for m in messages)


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
    assert any("does not call assert_preflight_result" in m for m in messages)


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
    assert any("does not call assert_preflight_result" in m for m in messages)


def test_a_fixture_parameter_shadows_the_assertion(tmp_path: Path) -> None:
    shadowed = f"{HEALTHY}\ndef test_healthy(assert_preflight_result):\n{ASSERT}"
    messages = _grade(tmp_path, _module(shadowed, LIFETIME))
    assert any("does not call assert_preflight_result" in m for m in messages)


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
    assert any("does not call assert_probe_lifetime" in m for m in messages)


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


# --- names and entrypoints ------------------------------------------------------

NEVER_CALLS = "does not call assert_preflight_result"
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


def test_an_unresolved_parametrize_withholds_a_function_marker(tmp_path: Path) -> None:
    decorated = (
        '@pytest.mark.parametrize("entrypoint", build_cases())\n'
        f"{HEALTHY}\ndef test_healthy(entrypoint):\n{ASSERT}"
    )
    messages = _grade(tmp_path, _module(decorated, LIFETIME))
    assert any(UNREADABLE in m for m in messages)
    assert any(NOT_REGISTERED in m for m in messages)


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


# --- the allowlist ------------------------------------------------------------
#
# F016 accepts the shapes atlan-openapi-app, atlan-mysql-app and
# atlan-metabase-app use, and reports anything else rather than modelling it.
# The only question it answers about the body is whether the test can *pass*
# without making the call; a statement that fails the test is the test gate's.


def _assert_collectable(module: dict[str, str]) -> None:
    """Import the generated module, so every decorator it applies is evaluated.

    A fixture whose decorator raises at import time is never collected by
    pytest, so it cannot show the reader telling a runnable test from a
    skipped one.
    """
    (source,) = module.values()
    namespace: dict[str, object] = {}
    with warnings.catch_warnings():
        # The marker is registered by the consumer's conftest, not here.
        warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
        exec(compile(source, "test_preflight.py", "exec"), namespace)  # noqa: S102
    assert all(
        callable(namespace[name]) for name in namespace if name.startswith("test_")
    )


def _loop(header: str, *after: str) -> str:
    """A loop whose body is the contract assertion, then *after*."""
    return (
        f"    {header}\n    " + ASSERT + "".join(f"        {line}\n" for line in after)
    )


#: One test per shape the three reference apps use, condensed. The
#: cancellation test's ``with pytest.raises(asyncio.CancelledError): await
#: task`` is the one the flow-modelling reader wrongly rejected in all three.
REFERENCE_APP_SHAPES = (
    PRELUDE
    + "import asyncio\nimport logging\nfrom unittest import mock\n\n"
    + """
async def _run(**kwargs):
    return None


@pytest.mark.asyncio
@mock.patch.dict("os.environ", {})
@pytest.mark.preflight_conformance(rule="F016", scenario="healthy")
async def test_healthy():
    result = await _run()
ASSERT


@pytest.mark.preflight_conformance(rule="F016", scenario="hung_probe")
async def test_hung(closed_clients):
    with pytest.raises(TimeoutError) as hung:
        await _run(budget=5)
    assert hung.value.__class__.__name__ == "TimeoutError"
    assert_probe_lifetime(elapsed=0, budget=5.0, background_stopped=bool(closed_clients))
    result = await _run(budget=5)
ASSERT


@pytest.mark.preflight_conformance(rule="F016", scenario="cancellation_cleanup")
async def test_cancellation(closed_clients):
    probing = asyncio.Event()
    task = asyncio.create_task(_run(budget=5))
    await asyncio.wait_for(probing.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_probe_lifetime(elapsed=0, budget=5.0, background_stopped=bool(closed_clients))
    result = await _run(budget=5)
ASSERT


@pytest.mark.preflight_conformance(rule="F016", scenario="budget_retry")
async def test_budget_retry(closed_clients):
    budgets = (60, 10, 2)
    for index, budget in enumerate(budgets, start=1):
        result = await _run(budget=budget)
    ASSERT
        assert_probe_lifetime(elapsed=0, budget=float(budget), background_stopped=len(closed_clients) == index)


@pytest.mark.preflight_conformance(rule="F016", scenario="credential_entrypoint_shapes")
async def test_shapes():
    shapes: list[tuple[dict, str]] = [({}, "ready"), ({"creds": []}, "not_ready")]
    for kwargs, expected in shapes:
        result = await _run(**kwargs)
    ASSERT
    for _ in range(2):
        result = await _run()
    ASSERT


@pytest.mark.preflight_conformance(rule="F016", scenario="extraction_fallback")
async def test_extraction_fallback(caplog):
    client = object()
    try:
        with pytest.raises(Exception) as probe_error:
            await _run(client=client)
    finally:
        await _run(close=client)
    with caplog.at_level(logging.DEBUG):
        result = await _run()
ASSERT
""".replace("\nASSERT\n", "\n" + ASSERT).replace("    ASSERT\n", "    " + ASSERT)
)


def test_every_reference_app_shape_is_credited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        SCENARIOS,
        "F016",
        (
            "healthy",
            "hung_probe",
            "cancellation_cleanup",
            "budget_retry",
            "credential_entrypoint_shapes",
            "extraction_fallback",
        ),
    )
    module = {"tests/unit/test_preflight.py": REFERENCE_APP_SHAPES}
    _assert_collectable(module)
    assert _grade(tmp_path, module) == []


@pytest.mark.parametrize(
    ("extra", "body"),
    [
        ("", _loop("for _ in range(2):")),
        ("", _loop("for _ in range(1, 3):")),
        ("", _loop("for case in ['a', 'b']:")),
        ("", "    cases = ('a',)\n" + _loop("for case in cases:")),
        (
            "",
            "    cases: list[str] = ['a']\n"
            + _loop("for index, case in enumerate(cases, start=1):"),
        ),
        ("", "    cases = ['a']\n    budget = 5\n" + _loop("for case in cases:")),
        ("", "    cases = ('a',)\n    print(cases)\n" + _loop("for case in cases:")),
        ("CASES = ('a', 'b')", _loop("for case in CASES:")),
        ("", _loop("for _ in range(2):", "break")),
        (
            "",
            "    for _ in range(2):\n        for inner in range(3):\n"
            "            break\n    " + ASSERT,
        ),
    ],
    ids=[
        "range",
        "range-bounds",
        "literal",
        "local-name",
        "annotated-enumerate",
        "untouched-local-list",
        "mentioned-local-tuple",
        "module-tuple",
        "break-after",
        "inner-loop-break",
    ],
)
def test_a_loop_that_provably_runs_its_body_is_credited(
    tmp_path: Path, extra: str, body: str
) -> None:
    module = _module(extra, _test(HEALTHY, body), LIFETIME)
    _assert_collectable(module)
    assert _grade(tmp_path, module) == []


@pytest.mark.parametrize(
    "body",
    [
        _loop("for case in load_cases():"),
        _loop("for case in []:"),
        _loop("for _ in range(0):"),
        _loop("for _ in range(attempts):"),
        "    cases = []\n" + _loop("for case in cases:"),
        "    cases = ['a']\n    cases = load()\n" + _loop("for case in cases:"),
        "    range = fake_range\n" + _loop("for _ in range(2):"),
        "    cases = [None]\n    cases.clear()\n" + _loop("for result in cases:"),
        "    cases = [None]\n    alias = cases\n    alias.clear()\n"
        + _loop("for result in cases:"),
        "    cases = [None]\n    drain(cases)\n" + _loop("for result in cases:"),
        "    cases = [None]\n    def later():\n        cases.pop()\n    later()\n"
        + _loop("for result in cases:"),
        _loop("while True:", "break"),
        "    for _ in range(2):\n        if skip_it():\n            continue\n    "
        + ASSERT,
        "    for _ in range(2):\n        break\n    " + ASSERT,
    ],
    ids=[
        "call",
        "empty-literal",
        "range-zero",
        "range-unknown",
        "empty-local",
        "rebound-local",
        "shadowed-range",
        "list-cleared",
        "list-cleared-through-alias",
        "list-passed-to-a-call",
        "list-mutated-by-a-closure",
        "while",
        "continue-before",
        "break-before",
    ],
)
def test_a_loop_that_may_skip_its_body_is_not_credited(
    tmp_path: Path, body: str
) -> None:
    messages = _grade(tmp_path, _module(_test(HEALTHY, body), LIFETIME))
    assert any(NEVER_CALLS in message for message in messages)


@pytest.mark.parametrize(
    "extra",
    [
        "CASES = [None]",
        "CASES = [None]\n\n\ndef test_drains():\n    CASES.clear()",
    ],
    ids=["module-list", "module-list-cleared-elsewhere"],
)
def test_a_module_level_list_does_not_prove_a_loop_runs(
    tmp_path: Path, extra: str
) -> None:
    """Any code that runs first — another test, a fixture, an import — can
    empty a module-level list; only a tuple stays non-empty."""
    body = _loop("for result in CASES:")
    module = _module(extra, _test(HEALTHY, body), LIFETIME)
    _assert_collectable(module)
    messages = _grade(tmp_path, module)
    assert any(NEVER_CALLS in message for message in messages)


@pytest.mark.parametrize(
    ("prelude", "body"),
    [
        (PRELUDE, "    if ready():\n        return\n" + ASSERT),
        (PRELUDE, "    with context():\n        return\n" + ASSERT),
        (PRELUDE, "    if windows():\n        pytest.skip('posix only')\n" + ASSERT),
        (PRELUDE, "    if flaky():\n        pytest.xfail('flaky')\n" + ASSERT),
        (PRELUDE, "    pytest.importorskip('respx')\n" + ASSERT),
        (PRELUDE, "    pytest.exit('stop')\n" + ASSERT),
        (
            PRELUDE + "from pytest import skip\n",
            "    if windows():\n        skip('posix only')\n" + ASSERT,
        ),
        (PRELUDE, "    yield\n" + ASSERT),
        (PRELUDE, ASSERT + "    yield\n"),
    ],
    ids=[
        "conditional-return",
        "return-in-with",
        "conditional-skip",
        "conditional-xfail",
        "importorskip",
        "exit",
        "imported-skip",
        "generator",
        "generator-after",
    ],
)
def test_a_way_to_pass_without_the_call_is_not_credited(
    tmp_path: Path, prelude: str, body: str
) -> None:
    messages = _grade(
        tmp_path, _module(_test(HEALTHY, body), LIFETIME, prelude=prelude)
    )
    assert any(NEVER_CALLS in message for message in messages)


@pytest.mark.parametrize(
    ("extra", "body"),
    [
        ("", "    if ready():\n    " + ASSERT),
        ("", "    if True:\n    " + ASSERT),
        ("", "    try:\n    " + ASSERT + "    finally:\n        cleanup()\n"),
        (
            "",
            "    try:\n        check_source()\n    except ExpectedError:\n    "
            + ASSERT,
        ),
        ("", "    with caplog.at_level(10):\n    " + ASSERT),
        ("", "    outcome = " + ASSERT.lstrip()),
        ("", "    [" + ASSERT.strip() + " for _ in range(1)]\n"),
        ("", "    def later():\n    " + ASSERT + "    later()\n"),
        ("def check(result):\n" + ASSERT, "    check(result)\n"),
    ],
    ids=[
        "if",
        "if-true",
        "try",
        "except-handler",
        "with",
        "assigned",
        "comprehension",
        "nested-def",
        "helper",
    ],
)
def test_a_call_outside_the_accepted_positions_is_not_credited(
    tmp_path: Path, extra: str, body: str
) -> None:
    messages = _grade(tmp_path, _module(extra, _test(HEALTHY, body), LIFETIME))
    assert any(NEVER_CALLS in message for message in messages)


def test_an_awaited_async_helper_is_not_credited(tmp_path: Path) -> None:
    helper = "async def check(result):\n" + ASSERT
    test = f"{HEALTHY}\nasync def test_healthy():\n    await check(result)\n"
    messages = _grade(tmp_path, _module(helper, test, LIFETIME))
    assert any(NEVER_CALLS in message for message in messages)


@pytest.mark.parametrize(
    "before",
    [
        "    with pytest.raises(ValueError):\n        pass\n",
        "    with pytest.raises(ValueError):\n        raise TypeError()\n",
        "    assert False\n",
        "    raise RuntimeError('not yet')\n",
    ],
    ids=["raises-saw-nothing", "raises-wrong-type", "assert-false", "raise"],
)
def test_a_statement_that_fails_the_test_does_not_withhold_credit(
    tmp_path: Path, before: str
) -> None:
    """Each of these fails the test before the call, so the test gate goes red.

    F016 guards against a test that passes without making the call; it does
    not second-guess one that fails, which the gate already reports.
    """
    assert _grade(tmp_path, _module(_test(HEALTHY, before + ASSERT), LIFETIME)) == []


@pytest.mark.parametrize(
    ("prelude", "decorator"),
    [
        (PRELUDE + "import respx\n", "@respx.mock"),
        (PRELUDE + "from unittest import mock\n", '@mock.patch("os.getcwd")'),
        (PRELUDE + "from helpers import retry\n", "@retry(3)"),
    ],
    ids=["module-attribute", "patch", "from-import"],
)
def test_an_imported_decorator_is_taken_not_to_skip(
    tmp_path: Path, prelude: str, decorator: str
) -> None:
    module = _module(_test(f"{decorator}\n{HEALTHY}"), LIFETIME, prelude=prelude)
    assert _grade(tmp_path, module) == []


@pytest.mark.parametrize(
    ("extra", "decorator"),
    [
        ("def identity(fn):\n    return fn", "@identity"),
        ("def identity():\n    return lambda fn: fn", "@identity()"),
        ("", "@(lambda fn: fn)"),
        ('skip_ci = pytest.mark.skipif(False, reason="ci")', "@skip_ci"),
        ('gate = pytest.mark.skip(reason="later")', "@gate"),
    ],
    ids=["local-def", "local-factory", "lambda", "mark-alias", "skip-alias"],
)
def test_a_decorator_defined_in_the_module_is_not_read(
    tmp_path: Path, extra: str, decorator: str
) -> None:
    module = _module(extra, _test(f"{decorator}\n{HEALTHY}"), LIFETIME)
    _assert_collectable(module)
    messages = _grade(tmp_path, module)
    assert any(UNREADABLE in message for message in messages)
    assert any(NOT_REGISTERED in message for message in messages)


@pytest.mark.parametrize(
    ("prelude", "extra"),
    [
        (
            "import pytest\nfrom mylib import assert_preflight_result\n"
            + PRELUDE.replace("import pytest\n", ""),
            "",
        ),
        (PRELUDE, "if False:\n    assert_preflight_result = print"),
        (
            PRELUDE,
            "try:\n    import fast\nexcept ImportError:\n    assert_preflight_result = print",
        ),
    ],
    ids=["imported-twice", "dead-branch-rebinding", "fallback-rebinding"],
)
def test_an_assertion_name_bound_twice_is_not_resolved(
    tmp_path: Path, prelude: str, extra: str
) -> None:
    messages = _grade(
        tmp_path, _module(extra, _test(HEALTHY), LIFETIME, prelude=prelude)
    )
    assert any(NEVER_CALLS in message for message in messages)


@pytest.mark.parametrize(
    "prelude",
    [PRELUDE + "import pytest\n", PRELUDE + "from helpers import *\n"],
    ids=["pytest-imported-twice", "star-import"],
)
def test_an_ambiguous_pytest_binding_leaves_the_marker_unresolved(
    tmp_path: Path, prelude: str
) -> None:
    messages = _grade(tmp_path, _module(_test(HEALTHY), LIFETIME, prelude=prelude))
    assert any("not statically resolvable" in message for message in messages)
    assert any(NOT_REGISTERED in message for message in messages)
