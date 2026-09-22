"""Grade the preflight scenarios from the test job's own run, once.

F016-F018 are not tests the suite owns. The tests are app-owned — e.g.
`atlan-openapi-app`'s `tests/unit/test_preflight_conformance.py`, 13
scenarios driving the real handler — and they already run in the app's
test job. What the suite owns is the *manifest* (`SCENARIOS`) and the
coverage verdict over it.

Until now the only way to reach that verdict was `--with-tests`, which
re-runs those same tests in a bounded subprocess, because the suite had
no way to see results the test job already produced. So the choice was:
run the scenarios twice, or have F016-F018 report `not_evaluated` and
F019 warn on a repo whose tests are green.

`--preflight-report` removes the choice. The producer is the ordinary
test run; the suite grades what came out of it:

    pytest -p conformance.preflight_testing --preflight-report=r.json
    detect --series F --preflight-report r.json

Two properties carry the design, and both are asserted below:

1. **Producing the report must not change what the test job runs.**
   The collection hook deselects non-scenario tests so the suite's own
   subprocess stays bounded. Applied to a normal run that would silently
   gut it — openapi's job would go from 256 tests to 13, taking the
   coverage floor with it. So deselection is now conditional on rule
   scoping, which only the subprocess passes.

2. **Absence of evidence is never evidence of conformance.** A missing,
   malformed or truncated report grades as an execution error, so the
   verdict is BLOCK, not silence. Verified end to end: a nonexistent
   path yields 14 findings and exit 1, not a clean run.

The grading itself is deliberately *not* duplicated for the new path —
`run_behavior` resolves `(execution, data)` first and interprets it in
one place — so a softer verdict cannot be reached by supplying evidence
from elsewhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conformance.preflight_testing import SCENARIOS, pytest_collection_modifyitems
from conformance.suite.checks.preflight._behavior import _load, run_behavior

_RULE = "F016"


# --------------------------------------------------------------------------
# _load: what counts as usable evidence
# --------------------------------------------------------------------------


def _write(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_accepts_a_well_formed_report(tmp_path: Path) -> None:
    payload = {"tests": {"t": {"rule": _RULE}}, "collection_errors": 0}
    execution, data = _load(_write(tmp_path, payload))
    assert execution == "completed"
    assert data == payload


@pytest.mark.parametrize(
    "name,payload",
    [
        ("empty object", {}),
        ("a list, not an object", [1, 2]),
        ("collection errors", {"tests": {}, "collection_errors": 2}),
    ],
)
def test_load_rejects_unusable_evidence(
    name: str, payload: Any, tmp_path: Path
) -> None:
    """Each of these would otherwise grade as a clean run with no scenarios."""
    execution, data = _load(_write(tmp_path, payload))
    assert execution == "error", name
    assert data == {}, name


def test_load_rejects_a_missing_file(tmp_path: Path) -> None:
    execution, data = _load(tmp_path / "never-written.json")
    assert execution == "error"
    assert data == {}


def test_load_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("{truncated", encoding="utf-8")
    execution, data = _load(path)
    assert execution == "error"
    assert data == {}


# --------------------------------------------------------------------------
# run_behavior(report=...): same grading, different provenance
# --------------------------------------------------------------------------


def _complete_report(rule: str = _RULE) -> dict[str, Any]:
    """A report in which every scenario the manifest requires passed."""
    phases = {"setup": "passed", "call": "passed", "teardown": "passed"}
    return {
        "tests": {
            f"tests/test_p.py::test_{scenario}": {
                "rule": rule,
                "scenario": scenario,
                "entrypoint": "default",
                "unsupported": False,
                "reason": False,
                "file": "tests/test_p.py",
                "line": index + 1,
                "phases": dict(phases),
            }
            for index, scenario in enumerate(SCENARIOS[rule])
        },
        "collection_errors": 0,
    }


def test_a_complete_report_grades_as_complete(tmp_path: Path) -> None:
    """The verdict that lets the F019 clearing pass drop its findings."""
    result = run_behavior(
        tmp_path, {_RULE}, report=_write(tmp_path, _complete_report())
    )
    assert result.findings == []
    assert result.summary[_RULE]["execution"] == "completed"
    assert result.summary[_RULE]["missing"] == []
    assert result.summary[_RULE]["complete"] is True
    assert result.summary[_RULE]["passed"] == len(SCENARIOS[_RULE])


def test_a_dropped_scenario_is_still_reported(tmp_path: Path) -> None:
    """Reading a report must not become a way to skip a scenario quietly."""
    payload = _complete_report()
    dropped = next(iter(payload["tests"]))
    scenario = payload["tests"].pop(dropped)["scenario"]
    result = run_behavior(tmp_path, {_RULE}, report=_write(tmp_path, payload))
    assert result.summary[_RULE]["complete"] is False
    assert f"default:{scenario}" in result.summary[_RULE]["missing"]
    assert any(scenario in f.message for f in result.findings)


def test_a_failed_scenario_is_still_reported(tmp_path: Path) -> None:
    payload = _complete_report()
    target = next(iter(payload["tests"]))
    payload["tests"][target]["phases"]["call"] = "failed"
    result = run_behavior(tmp_path, {_RULE}, report=_write(tmp_path, payload))
    assert result.summary[_RULE]["complete"] is False
    assert result.findings


def test_an_unreadable_report_blocks_rather_than_passes(tmp_path: Path) -> None:
    """The fail-closed property, at the unit boundary."""
    result = run_behavior(tmp_path, {_RULE}, report=tmp_path / "absent.json")
    assert result.summary[_RULE]["execution"] == "error"
    assert result.summary[_RULE]["complete"] is False
    # Every required scenario, plus the execution finding itself.
    assert len(result.findings) == len(SCENARIOS[_RULE]) + 1


def test_reading_a_report_runs_no_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: no second execution.

    Asserted by making execution impossible rather than by timing it, so
    the test states the invariant instead of approximating it.
    """
    import conformance.suite.checks.preflight._behavior as behavior

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("run_behavior spawned a subprocess for a report path")

    monkeypatch.setattr(behavior.subprocess, "Popen", _forbidden)
    result = run_behavior(
        tmp_path, {_RULE}, report=_write(tmp_path, _complete_report())
    )
    assert result.summary[_RULE]["complete"] is True


# --------------------------------------------------------------------------
# The collection hook: producing a report must not change the test run
# --------------------------------------------------------------------------


class _Marker:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _Item:
    def __init__(self, nodeid: str, marker: _Marker | None) -> None:
        self.nodeid = nodeid
        self.location = ("tests/test_p.py", 0)
        self._marker = marker

    def get_closest_marker(self, name: str) -> _Marker | None:
        return self._marker if name == "preflight_conformance" else None


class _Hook:
    def __init__(self) -> None:
        self.deselected: list[Any] = []

    def pytest_deselected(self, items: list[Any]) -> None:
        self.deselected.extend(items)


class _Config:
    def __init__(self, report: str | None, rules: str | None) -> None:
        self._options = {"--preflight-report": report, "--preflight-rules": rules}
        self._preflight_evidence: dict[str, Any] = {"tests": {}, "collection_errors": 0}
        self.hook = _Hook()

    def getoption(self, name: str) -> Any:
        return self._options[name]


def _items() -> list[_Item]:
    return [
        _Item("::test_healthy", _Marker(rule=_RULE, scenario="healthy")),
        _Item("::test_unrelated", None),
        _Item("::test_other_rule", _Marker(rule="F017", scenario="not_ready")),
    ]


def test_unscoped_run_deselects_nothing() -> None:
    """The producer case. openapi's job must stay at 256 tests, not 13."""
    config = _Config("r.json", None)
    items = _items()
    original = list(items)
    pytest_collection_modifyitems(config, items)
    assert items == original, "a normal test run must keep every collected test"
    assert config.hook.deselected == []
    # Still records the marked scenarios — that is the by-product.
    recorded = config._preflight_evidence["tests"]
    assert set(recorded) == {"::test_healthy", "::test_other_rule"}


def test_scoped_run_still_deselects() -> None:
    """The suite's own subprocess stays bounded to the rules it asked for."""
    config = _Config("r.json", _RULE)
    items = _items()
    pytest_collection_modifyitems(config, items)
    assert [item.nodeid for item in items] == ["::test_healthy"]
    assert {item.nodeid for item in config.hook.deselected} == {
        "::test_unrelated",
        "::test_other_rule",
    }
    assert set(config._preflight_evidence["tests"]) == {"::test_healthy"}


def test_no_report_requested_is_a_no_op() -> None:
    """Without the flag the plugin must not touch collection at all."""
    config = _Config(None, None)
    items = _items()
    original = list(items)
    pytest_collection_modifyitems(config, items)
    assert items == original
    assert config.hook.deselected == []
    assert config._preflight_evidence["tests"] == {}
