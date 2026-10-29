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

plan_shards = certify_unit_shards.plan_shards
pytest_command = certify_unit_shards.pytest_command
run_shard = certify_unit_shards.run_shard
decide_verdict = certify_unit_shards.decide_verdict
main = certify_unit_shards.main
TARGET = certify_unit_shards.TARGET_SECONDS_PER_SHARD
MAX_SHARDS = certify_unit_shards.MAX_SHARDS


def _durations(tmp_path: Path, mapping: object) -> Path:
    path = tmp_path / ".test_durations"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


# ── plan ────────────────────────────────────────────────────────────────────


def test_no_durations_file_keeps_one_shard(tmp_path: Path) -> None:
    assert plan_shards(tmp_path / ".test_durations", 0) == 1


@pytest.mark.parametrize(
    ("unit_seconds", "expected"),
    [
        (1.0, 1),
        (TARGET, 1),
        (TARGET + 1, 2),
        (TARGET * 7 - 1, 7),
        (TARGET * 50, MAX_SHARDS),
    ],
)
def test_durations_size_the_shard_count(
    tmp_path: Path, unit_seconds: float, expected: int
) -> None:
    path = _durations(tmp_path, {"tests/unit/test_a.py::test_x": unit_seconds})
    assert plan_shards(path, 0) == expected


def test_only_unit_tier_durations_count(tmp_path: Path) -> None:
    # Certify runs tests/unit only; an integration suite in the same file must
    # not buy shards that would sit empty.
    path = _durations(
        tmp_path,
        {
            "tests/unit/test_a.py::test_x": 10.0,
            "tests/integration/test_b.py::test_y": TARGET * 9,
        },
    )
    assert plan_shards(path, 0) == 1


@pytest.mark.parametrize("content", ["not json", "[1, 2, 3]"])
def test_unusable_durations_file_falls_back_to_one_shard(
    tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / ".test_durations"
    path.write_text(content, encoding="utf-8")
    assert plan_shards(path, 0) == 1
    assert "::warning::" in capsys.readouterr().out


def test_override_wins_over_durations(tmp_path: Path) -> None:
    path = _durations(tmp_path, {"tests/unit/test_a.py::test_x": TARGET * 9})
    assert plan_shards(path, 3) == 3


@pytest.mark.parametrize("override", [-1, MAX_SHARDS + 1])
def test_override_out_of_range_is_rejected(tmp_path: Path, override: int) -> None:
    with pytest.raises(ValueError):
        plan_shards(tmp_path / ".test_durations", override)


def test_plan_writes_the_matrix_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    path = _durations(tmp_path, {"tests/unit/test_a.py::test_x": TARGET * 3})
    assert main(["plan", "--durations", str(path), "--override", "0"]) == 0
    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines == ["total=3", "shards=[1, 2, 3]"]
    assert json.loads(lines[1].split("=", 1)[1]) == [1, 2, 3]


def test_plan_fails_on_invalid_override(tmp_path: Path) -> None:
    assert main(["plan", "--durations", str(tmp_path / "x"), "--override", "99"]) == 1


# ── run ─────────────────────────────────────────────────────────────────────


def test_single_shard_command_has_no_split_flags() -> None:
    command = pytest_command(1, 1, "app")
    assert "--splits" not in command
    assert "pytest-split" not in command
    assert command[-3:] == ["tests/unit", "--cov=app", "--cov-report=term"]


def test_multi_shard_command_selects_its_group() -> None:
    command = pytest_command(2, 4, "my_pkg")
    assert command[:7] == [
        "uv",
        "run",
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
    ("code", "expected"),
    [(0, "success"), (5, "success"), (1, "failure"), (2, "failure")],
)
def test_run_maps_pytest_exit_codes(tmp_path: Path, code: int, expected: str) -> None:
    app, results = _app(tmp_path), tmp_path / "results"
    fake = _FakePytest(app, code)
    assert run_shard(3, 4, "app", "success", results, app, fake) == expected
    assert _result(results, 3) == expected
    # Coverage moves out under a per-shard name, so merge-multiple can't collide.
    assert (results / "coverage.shard-3").is_file()
    assert not (app / ".coverage").exists()


def test_run_without_coverage_data_still_records_outcome(tmp_path: Path) -> None:
    app, results = _app(tmp_path), tmp_path / "results"
    fake = _FakePytest(app, 5, writes_coverage=False)
    assert run_shard(1, 2, "app", "success", results, app, fake) == "success"
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
        ({1: "skipped", 2: "skipped", 3: "skipped"}, "skipped"),
        ({1: "success", 2: "install_failed", 3: "success"}, "skipped"),
        # A real failure outranks an install problem elsewhere.
        ({1: "failure", 2: "install_failed", 3: "success"}, "failure"),
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
