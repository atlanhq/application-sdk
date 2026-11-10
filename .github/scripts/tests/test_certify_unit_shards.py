"""Tests for .github/scripts/certify_unit_shards.py."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "certify_unit_shards.py"
_spec = importlib.util.spec_from_file_location("certify_unit_shards", _MODULE_PATH)
assert _spec and _spec.loader
certify_unit_shards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(certify_unit_shards)

pytest_command = certify_unit_shards.pytest_command
run_shard = certify_unit_shards.run_shard
decide_verdict = certify_unit_shards.decide_verdict
main = certify_unit_shards.main


# ── run ─────────────────────────────────────────────────────────────────────


def test_single_shard_command_has_no_split_flags() -> None:
    command = pytest_command(1, 1, "app")
    assert "--splits" not in command
    assert "pytest-split" not in command
    assert command[-3:] == ["tests/unit", "--cov=app", "--cov-report=term"]


def test_multi_shard_command_selects_its_group() -> None:
    command = pytest_command(2, 4, "my_pkg")
    assert command[:8] == [
        "uv",
        "run",
        "--frozen",
        "--all-groups",
        "--with",
        "pytest-cov",
        "--with",
        "pytest-split",
    ]
    assert "--cov=my_pkg" in command
    tail = command[command.index("--splits") :]
    assert tail == [
        "--splits",
        "4",
        "--group",
        "2",
        "--splitting-algorithm",
        "least_duration",
    ]


class _FakePytest:
    """Stub for the pytest subprocess: records argv, writes .coverage, returns a code."""

    def __init__(self, app_dir: Path, code: int, writes_coverage: bool = True) -> None:
        self.app_dir = app_dir
        self.code = code
        self.writes_coverage = writes_coverage
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> int:
        self.calls.append(list(command))
        if self.writes_coverage:
            (self.app_dir / ".coverage").write_text("data", encoding="utf-8")
        return self.code


def _app(tmp_path: Path, with_tests: bool = True) -> Path:
    app = tmp_path / "app"
    (app / "tests" / "unit" if with_tests else app).mkdir(parents=True)
    return app


def _result(results: Path, shard: int) -> str:
    return json.loads((results / f"shard-{shard}.json").read_text(encoding="utf-8"))[
        "outcome"
    ]


@pytest.mark.parametrize(
    ("code", "total", "expected"),
    [
        (0, 4, "success"),
        # One shard of several can legitimately be empty.
        (5, 4, "no_tests"),
        (1, 4, "failure"),
        (2, 4, "failure"),
        (0, 1, "success"),
        # A single shard that collects nothing is the whole suite collecting
        # nothing: that failed the gate before sharding and must still fail.
        (5, 1, "failure"),
        (1, 1, "failure"),
    ],
)
def test_run_maps_pytest_exit_codes(
    tmp_path: Path, code: int, total: int, expected: str
) -> None:
    app, results = _app(tmp_path), tmp_path / "results"
    fake = _FakePytest(app, code)
    shard = min(3, total)
    assert run_shard(shard, total, "app", "success", results, app, fake) == expected
    assert _result(results, shard) == expected
    # Coverage moves out under a per-shard name, so merge-multiple can't collide.
    assert (results / f"coverage.shard-{shard}").is_file()
    assert not (app / ".coverage").exists()


def test_run_without_coverage_data_still_records_outcome(tmp_path: Path) -> None:
    app, results = _app(tmp_path), tmp_path / "results"
    fake = _FakePytest(app, 5, writes_coverage=False)
    assert run_shard(1, 2, "app", "success", results, app, fake) == "no_tests"
    assert not (results / "coverage.shard-1").exists()


def test_run_skips_without_unit_tests_dir(tmp_path: Path) -> None:
    app, results = _app(tmp_path, with_tests=False), tmp_path / "results"
    fake = _FakePytest(app, 0)
    assert run_shard(1, 1, "app", "success", results, app, fake) == "skipped"
    assert fake.calls == []


def test_run_records_install_failure_without_running(tmp_path: Path) -> None:
    app, results = _app(tmp_path), tmp_path / "results"
    fake = _FakePytest(app, 0)
    assert run_shard(1, 1, "app", "failure", results, app, fake) == "install_failed"
    assert fake.calls == []
    assert _result(results, 1) == "install_failed"


def test_run_exit_code_is_nonzero_only_on_test_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    monkeypatch.chdir(app)
    argv = ["run", "--shard", "1", "--total", "1", "--coverage-source", "app"]
    argv += ["--install-outcome", "success", "--results-dir", str(tmp_path / "r")]
    assert main(argv, runner=_FakePytest(app, 1)) == 1
    assert main(argv, runner=_FakePytest(app, 0)) == 0


# ── verdict ─────────────────────────────────────────────────────────────────


def _write_results(results: Path, outcomes: dict[int, str]) -> None:
    results.mkdir(parents=True, exist_ok=True)
    for shard, outcome in outcomes.items():
        (results / f"shard-{shard}.json").write_text(
            json.dumps({"shard": shard, "outcome": outcome}), encoding="utf-8"
        )


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ({1: "success", 2: "success", 3: "success"}, "success"),
        ({1: "success", 2: "failure", 3: "success"}, "failure"),
        # No tests/unit anywhere: passes, an app may have no unit tests.
        ({1: "skipped", 2: "skipped", 3: "skipped"}, "skipped"),
        # A shard whose deps did not install never ran its tests: kept apart
        # from "skipped" so the certification verdict can fail closed on it.
        ({1: "success", 2: "install_failed", 3: "success"}, "install_failed"),
        (
            {1: "install_failed", 2: "install_failed", 3: "install_failed"},
            "install_failed",
        ),
        # A real failure outranks an install problem elsewhere.
        ({1: "failure", 2: "install_failed", 3: "success"}, "failure"),
        # Some empty shards are fine: more shards than tests.
        ({1: "success", 2: "no_tests", 3: "no_tests"}, "success"),
        # Every shard empty: the whole suite collected nothing.
        ({1: "no_tests", 2: "no_tests", 3: "no_tests"}, "failure"),
    ],
)
def test_verdict_folds_shard_outcomes(
    tmp_path: Path, outcomes: dict[int, str], expected: str
) -> None:
    _write_results(tmp_path, outcomes)
    assert decide_verdict(tmp_path, 3)[0] == expected


def test_verdict_fails_when_a_shard_result_is_missing(tmp_path: Path) -> None:
    _write_results(tmp_path, {1: "success", 3: "success"})
    outcome, reasons = decide_verdict(tmp_path, 3)
    assert outcome == "failure"
    assert "[2]" in reasons[0]


@pytest.mark.parametrize("total", [0, -1, 11])
def test_verdict_fails_on_a_shard_count_outside_the_range(
    tmp_path: Path, total: int
) -> None:
    # No matrix entry -> no leg runs -> no results. With total 0, "no missing
    # shards" and all() over nothing would otherwise read as skipped, which
    # does not block publish.
    outcome, reasons = decide_verdict(tmp_path / "absent", total)
    assert outcome == "failure"
    assert str(total) in reasons[0]


def test_verdict_fails_on_a_zero_count_even_via_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    argv = ["verdict", "--results-dir", str(tmp_path / "absent"), "--total", "0"]
    assert main(argv) == 0
    assert output.read_text(encoding="utf-8") == "outcome=failure\n"


def test_verdict_fails_with_no_results_at_all(tmp_path: Path) -> None:
    assert decide_verdict(tmp_path / "absent", 2)[0] == "failure"


def test_verdict_fails_on_an_unreadable_result(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "shard-1.json").write_text("{", encoding="utf-8")
    assert decide_verdict(tmp_path, 1)[0] == "failure"


def test_verdict_writes_outcome_and_never_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The certification verdict step enforces; this step only reports, so the
    # summary still renders every layer on a failing run.
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    results = tmp_path / "results"
    _write_results(results, {1: "failure"})
    assert main(["verdict", "--results-dir", str(results), "--total", "1"]) == 0
    assert output.read_text(encoding="utf-8") == "outcome=failure\n"


@pytest.mark.parametrize(
    ("outcomes", "written", "annotation"),
    [
        ({1: "install_failed"}, "outcome=install_failed\n", "::error::"),
        ({1: "skipped"}, "outcome=skipped\n", ""),
    ],
)
def test_verdict_reports_install_failure_distinct_from_no_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcomes: dict[int, str],
    written: str,
    annotation: str,
) -> None:
    # The two non-runs reach the certification verdict as different outputs:
    # it blocks on install_failed and passes on skipped (FND-4538).
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    results = tmp_path / "results"
    _write_results(results, outcomes)
    assert main(["verdict", "--results-dir", str(results), "--total", "1"]) == 0
    assert output.read_text(encoding="utf-8") == written
    out = capsys.readouterr().out
    if annotation:
        assert annotation in out
    else:
        assert "::error::" not in out and "::warning::" not in out


def test_pytest_command_never_relocks() -> None:
    """The shard has no git auth (FND-4739): it must use what the install
    step synced, never re-lock and fetch."""
    for total in (1, 3):
        assert pytest_command(1, total, "app")[:3] == ["uv", "run", "--frozen"]
