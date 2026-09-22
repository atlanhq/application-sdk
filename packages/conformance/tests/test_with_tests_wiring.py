"""`--with-tests` has to be wired end to end, not just declared somewhere.

The F-series is the preflight gate, and by default it runs static-only:
the TEST rules (F016-F018) report as not evaluated, and F019 reports the
value-level shapes static analysis cannot resolve. A conforming handler
therefore still draws an F019 WARN on a static run — atlan-openapi-app's
`app/handler.py` is the case that surfaced this, and it is the rule's own
`canonical_reference`, so "fix the app" was never the remedy. Executing
the registered scenarios is, and `detect` already has the flag for it.

What was missing was a way for a caller to ask for it. That path has four
links, and a break in any one of them is silent — the leg just keeps
running static and the warning stays:

    caller's conformance.yaml
      -> conformance-reusable.yaml     `with-tests` input
      -> run-conformance-detect        `with-tests` input -> WITH_TESTS env
      -> build_conformance_args.py     WITH_TESTS -> `--with-tests`
      -> detect

String-contains assertions cannot see a break here: `"with-tests" in
content` passes just as happily when the input is declared but never
forwarded, when it is forwarded to a step that does not export it, or
when only three of the four detect flavours carry the env. So each link
is asserted structurally, off the parsed YAML and the real function.

The fourth link is the one with a cost: the scenarios import the app, so
under `--with-tests` the F leg must also materialise the caller's
environment — the same reason the D leg sets `needs_env`. Tying the two
together is what stops `with-tests: true` from running pytest against an
env that was never synced.

Note on rollout: the reusable workflow forwards `with-tests` to the
vendored composite action unconditionally, and consumers hold their own
copy of that action (C002 drift is WARN-only, so a stale copy can persist
indefinitely). A consumer that has not re-synced gets a
`##[warning] Unexpected input(s) 'with-tests'` annotation on its
conformance legs and the flag is ignored, which is exactly today's
behaviour. Noisy during the transition, not breaking.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml
from conformance.bootstrap.render import render

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The reusable workflow the fleet calls, and the action it delegates to.
_SUITE = _REPO_ROOT / ".github/workflows/conformance-reusable.yaml"
_ACTION_TEMPLATE = "run-conformance-detect-action.yaml"

#: The series whose scenarios `--with-tests` executes. Read from the matrix
#: rather than assumed, so moving preflight to another letter fails here.
_PREFLIGHT_SERIES = "F"

#: The env var the action exports and the script reads. One name, asserted
#: on both sides, so a rename cannot pass by only landing on one of them.
_ENV_VAR = "WITH_TESTS"

#: The input name, spelled as YAML sees it on both the workflow and action.
_INPUT = "with-tests"


def _load_build_args_module() -> Any:
    """Import the vendored arg builder from its canonical monorepo path.

    Loaded by path, not imported as a package: the file ships as a
    standalone script into consumer repos (`.github/scripts/`), so it has
    no importable parent and the test must reach it the way the action
    does — via the file itself.
    """
    path = _REPO_ROOT / ".github/scripts/build_conformance_args.py"
    spec = importlib.util.spec_from_file_location("_bca", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _suite_doc() -> dict[str, Any]:
    return yaml.safe_load(_SUITE.read_text(encoding="utf-8"))


def _suite_inputs() -> dict[str, Any]:
    # `on:` parses as the boolean True in YAML 1.1, which is why this is
    # keyed off True rather than the string.
    return _suite_doc()[True]["workflow_call"]["inputs"]


def _matrix_legs() -> list[dict[str, Any]]:
    for job in _suite_doc()["jobs"].values():
        include = job.get("strategy", {}).get("matrix", {}).get("include")
        if include:
            return include
    raise AssertionError("no matrix job with an `include` list in the suite")


def _detect_step() -> dict[str, Any]:
    for job in _suite_doc()["jobs"].values():
        for step in job.get("steps", []) or []:
            uses = str(step.get("uses", "")) if isinstance(step, dict) else ""
            if "run-conformance-detect" in uses:
                return step
    raise AssertionError("no step using run-conformance-detect in the suite")


def _action_doc() -> dict[str, Any]:
    return yaml.safe_load(render(_ACTION_TEMPLATE))


def _detect_steps_of_action() -> list[dict[str, Any]]:
    """Every invocation flavour in the composite action.

    Identified by the step that actually runs the arg builder, so adding a
    fifth flavour brings it under these assertions automatically instead of
    quietly shipping without the env var.
    """
    steps = [
        step
        for step in _action_doc()["runs"]["steps"]
        if "build_conformance_args.py" in str(step.get("run", ""))
    ]
    assert steps, "no step in the action invokes build_conformance_args.py"
    return steps


# --------------------------------------------------------------------------
# Link 4: the script
# --------------------------------------------------------------------------


def test_build_args_omits_the_flag_by_default() -> None:
    """Static-only stays the default; nothing opts in implicitly."""
    args = _load_build_args_module().build_args("F", "preflight")
    assert "--with-tests" not in args


def test_build_args_emits_the_flag_when_asked() -> None:
    args = _load_build_args_module().build_args("F", "preflight", with_tests=True)
    assert "--with-tests" in args


def test_build_args_never_emits_static() -> None:
    """`--static` and `--with-tests` are mutually exclusive on the runner.

    The script must never emit `--static` explicitly — it is the runner's
    default — or the two could be passed together and the runner would
    reject the whole invocation.
    """
    module = _load_build_args_module()
    for kwargs in ({}, {"with_tests": True}):
        assert "--static" not in module.build_args("F", "preflight", **kwargs)


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False)])
def test_main_reads_the_env_var(
    value: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The action passes the flag as env, not argv, so main must read it."""
    module = _load_build_args_module()
    monkeypatch.setenv(_ENV_VAR, value)
    assert module.main(["--series", "F", "--slug", "preflight"]) == 0
    emitted = capsys.readouterr().out.split()
    assert ("--with-tests" in emitted) is expected


# --------------------------------------------------------------------------
# Link 3: the composite action
# --------------------------------------------------------------------------


def test_action_declares_the_input_defaulting_off() -> None:
    declared = _action_doc()["inputs"]
    assert _INPUT in declared, sorted(declared)
    # Composite-action inputs are strings; "false" is the off value the
    # step conditions and the script's `.lower() == "true"` both expect.
    assert declared[_INPUT]["default"] == "false"


def test_every_detect_flavour_exports_the_env_var() -> None:
    """All four invocation flavours, not just the one being tested by hand."""
    for step in _detect_steps_of_action():
        env = step.get("env", {})
        assert _ENV_VAR in env, f"{step.get('name')!r} does not export {_ENV_VAR}"
        assert env[_ENV_VAR] == "${{ inputs." + _INPUT + " }}", step.get("name")


def test_env_var_is_exported_wherever_exit_zero_is() -> None:
    """Pin the new env var to the set that already reaches the script.

    EXIT_ZERO travels the same path for the same reason, so asserting the
    two sets are equal means a future flavour cannot pick up one and miss
    the other.
    """
    with_flag = {
        step.get("name")
        for step in _detect_steps_of_action()
        if _ENV_VAR in step.get("env", {})
    }
    with_exit_zero = {
        step.get("name")
        for step in _detect_steps_of_action()
        if "EXIT_ZERO" in step.get("env", {})
    }
    assert with_flag == with_exit_zero


# --------------------------------------------------------------------------
# Links 1 and 2: the reusable workflow
# --------------------------------------------------------------------------


def test_suite_declares_the_input_as_a_boolean_defaulting_off() -> None:
    declared = _suite_inputs()
    assert _INPUT in declared, sorted(declared)
    assert declared[_INPUT]["type"] == "boolean"
    assert declared[_INPUT]["default"] is False
    assert declared[_INPUT]["required"] is False


def test_suite_forwards_the_input_to_the_action() -> None:
    """Declared but unforwarded is the silent break this exists to catch."""
    assert _detect_step()["with"][_INPUT] == "${{ inputs." + _INPUT + " }}"


def test_preflight_leg_ties_its_environment_to_the_input() -> None:
    """The scenarios import the app, so opting in must also sync the env.

    Asserted as an expression referencing the input, not as a literal:
    hardcoding `needs_env: "true"` would sync on every static run too and
    make the cheap default expensive for the whole fleet.
    """
    legs = {leg["series"]: leg for leg in _matrix_legs()}
    assert _PREFLIGHT_SERIES in legs, sorted(legs)
    needs_env = legs[_PREFLIGHT_SERIES].get("needs_env")
    assert _INPUT in str(needs_env), needs_env


def test_every_needs_env_resolves_to_a_string() -> None:
    """No leg may hand `matrix.needs_env` a boolean.

    `matrix.needs_env == 'true'` guards the system-deps install step, and
    GHA casts both sides to a number when their types differ: boolean
    `true` becomes 1 and `'true'` becomes NaN, so the comparison is false.
    A leg spelling `needs_env: ${{ inputs.with-tests }}` therefore skips
    the apt-get step on exactly the repos that declared native deps, and
    `uv sync` fails on that leg — while the composite action still sees
    "true", because action inputs stringify. That asymmetry is what makes
    the bug silent, so it is pinned here rather than left to review.

    Enforced syntactically: a value is acceptable only if it is a quoted
    literal, or an expression whose branches are quoted literals. A bare
    `${{ <ref> }}` passthrough is rejected whatever it references.
    """
    offenders = {}
    for leg in _matrix_legs():
        raw = leg.get("needs_env")
        if raw is None:
            continue  # leg opts out entirely; nothing to coerce
        value = str(raw)
        if "${{" not in value:
            # A plain YAML scalar. It must be the string, not a bool.
            if not isinstance(raw, str):
                offenders[leg["series"]] = f"{raw!r} is {type(raw).__name__}"
            continue
        # An expression: both branches have to be quoted string literals.
        if "'true'" not in value or "'false'" not in value:
            offenders[leg["series"]] = value
    assert not offenders, (
        "these legs hand matrix.needs_env a value that can resolve to a "
        f"boolean, which silently fails `== 'true'`: {offenders}. Spell it "
        "`${{ <cond> && 'true' || 'false' }}`."
    )


def test_no_other_leg_opted_into_the_input() -> None:
    """Only the preflight leg has scenarios; nothing else should pay for it."""
    others = {
        leg["series"]: leg.get("needs_env")
        for leg in _matrix_legs()
        if leg["series"] != _PREFLIGHT_SERIES
        and _INPUT in str(leg.get("needs_env", ""))
    }
    assert not others, others
