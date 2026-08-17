"""Tests for the scorecard argument builder (FND-33 / FND-34).

The three conditional arguments each encode a decision that is easy to reverse
by accident and impossible to notice afterwards — a scorecard that scored an
absent tier as zero, or recorded cross-CSP coverage it never observed, looks
exactly like a correct one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "build_scorecard_args.py"

_spec = importlib.util.spec_from_file_location("build_scorecard_args", _SCRIPT)
assert _spec and _spec.loader
build_scorecard_args = importlib.util.module_from_spec(_spec)
sys.modules["build_scorecard_args"] = build_scorecard_args
_spec.loader.exec_module(build_scorecard_args)

build_args = build_scorecard_args.build_args
e2e_ran = build_scorecard_args.e2e_ran

_BASE = {
    "repo": "atlanhq/atlan-openapi-app",
    "commit": "deadbeef",
    "out": "results/test-readiness.json",
    "unit_junit": "unit-evidence/results/test-results.xml",
    "unit_coverage": "unit-evidence/coverage.json",
    "integration_junit": "integration-evidence/results/test-results.xml",
    "integration_coverage": "integration-evidence/coverage.json",
    "e2e_junit_glob": "e2e-evidence/*/results/sdr-test-results.xml",
    "e2e_result": "skipped",
    "configured_clouds": "",
    "configured_known": False,
    "observed_clouds": "",
}


def _args(**overrides: object) -> list[str]:
    return build_args(**{**_BASE, **overrides})  # type: ignore[arg-type]


def _value_after(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


# ── The always-on tiers ──────────────────────────────────────────────────────


def test_unit_and_integration_are_always_passed() -> None:
    # A missing integration junit scores an empty tier ON PURPOSE: an app with no
    # integration suite must still have that count against its grade.
    args = _args()
    assert _value_after(args, "--unit-junit") == _BASE["unit_junit"]
    assert _value_after(args, "--integration-junit") == _BASE["integration_junit"]
    assert _value_after(args, "--repo") == _BASE["repo"]
    assert _value_after(args, "--commit") == "deadbeef"
    assert _value_after(args, "--out") == _BASE["out"]


# ── e2e evidence: absent must never read as zero ─────────────────────────────


@pytest.mark.parametrize("result", ["skipped", "", "  ", "SKIPPED"])
def test_a_skipped_e2e_passes_no_evidence_flags(result: str) -> None:
    """The routine push. Omitting the flag leaves the tier not-applicable.

    Passing an empty glob instead would still work — the CLI treats "matched
    nothing" as not-applicable — but "the job was skipped" is knowable here, and
    two ways of learning the same fact are two ways to drift.
    """
    args = _args(e2e_result=result)
    assert "--e2e-junit" not in args
    assert "--cross-cloud-observed" not in args


@pytest.mark.parametrize("result", ["success", "failure", "cancelled"])
def test_a_failed_e2e_still_contributes_evidence(result: str) -> None:
    """A red e2e run is evidence.

    Suppressing it would let an app's scorecard IMPROVE by having its e2e break
    — the tier would go not-applicable and its weight renormalize away.
    """
    args = _args(e2e_result=result)
    assert _value_after(args, "--e2e-junit") == _BASE["e2e_junit_glob"]
    assert e2e_ran(result) is True


def test_no_glob_configured_passes_no_e2e_junit() -> None:
    assert "--e2e-junit" not in _args(e2e_result="success", e2e_junit_glob="")


# ── cross-CSP: configured is the rollout signal, observed the verification one ─


def test_configured_lands_even_when_e2e_did_not_run() -> None:
    """The whole point of `configured`: it needs no e2e run.

    It is the only field that reaches central visibility on the routine
    push/merge_group path, where e2e is skipped.
    """
    args = _args(configured_known=True, configured_clouds="aws,azure,gcp")
    assert _value_after(args, "--cross-cloud-configured") == "aws,azure,gcp"
    assert "--cross-cloud-observed" not in args


def test_configured_empty_is_recorded_not_omitted() -> None:
    """ "Repo has no tenant matrix" is a fact — the degraded state, not unknown.

    Collapsing it into "unknown" makes a repo nobody onboarded indistinguishable
    from one that is fully covered, which is the gap FND-34 exists to close.
    """
    args = _args(configured_known=True, configured_clouds="")
    assert "--cross-cloud-configured" in args
    assert _value_after(args, "--cross-cloud-configured") == ""


def test_configured_is_omitted_when_resolution_failed() -> None:
    """Resolution failed (or e2e is disabled for the repo) → say nothing."""
    assert "--cross-cloud-configured" not in _args(configured_known=False)


def test_observed_rides_only_on_runs_where_e2e_ran() -> None:
    args = _args(e2e_result="success", observed_clouds="aws,azure")
    assert _value_after(args, "--cross-cloud-observed") == "aws,azure"


def test_observed_empty_records_the_single_tenant_fallback() -> None:
    """e2e ran with no cloud dimension — degraded, and distinct from "did not run".

    The flag is still passed, so the CLI records `observed: []`. Dropping the
    flag here would make a degraded run indistinguishable from a run where e2e
    never happened.
    """
    args = _args(e2e_result="success", observed_clouds="")
    assert "--cross-cloud-observed" in args
    assert _value_after(args, "--cross-cloud-observed") == ""


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"configured_known": True, "configured_clouds": ""},
        {"e2e_result": "success", "observed_clouds": ""},
        {
            "e2e_result": "success",
            "observed_clouds": "",
            "configured_known": True,
            "configured_clouds": "",
        },
    ],
)
def test_the_last_argument_is_never_empty(overrides: dict[str, object]) -> None:
    """The caller reads this with `mapfile`, and a trailing empty line is fragile.

    Either cross-CSP value may legitimately be "", and a `raw=$(...)` capture
    strips trailing newlines — which would drop that element and leave
    `--cross-cloud-observed` dangling, so argparse would eat the NEXT flag as
    its value. Ending on a non-empty argument removes the dependency entirely.
    """
    assert _args(**overrides)[-1] != ""


# ── The env-driven entrypoint ────────────────────────────────────────────────


def test_main_reads_the_environment_and_prints_one_arg_per_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in {
        "REPO": "atlanhq/atlan-openapi-app",
        "SHA": "cafe",
        "UNIT_JUNIT": "u.xml",
        "UNIT_COVERAGE": "u.json",
        "INTEGRATION_JUNIT": "i.xml",
        "INTEGRATION_COVERAGE": "i.json",
        "E2E_JUNIT_GLOB": "e2e-evidence/*/results/sdr-test-results.xml",
        "E2E_RESULT": "success",
        "CONFIGURED_CLOUDS": "aws,gcp",
        "CONFIGURED_KNOWN": "true",
        "OBSERVED_CLOUDS": "aws",
    }.items():
        monkeypatch.setenv(key, value)

    assert build_scorecard_args.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert _value_after(lines, "--cross-cloud-configured") == "aws,gcp"
    assert _value_after(lines, "--cross-cloud-observed") == "aws"
    assert _value_after(lines, "--e2e-junit").endswith("sdr-test-results.xml")
    # Default when OUT is unset, so a caller that forgets it still writes where
    # the upload step looks.
    assert _value_after(lines, "--out") == "results/test-readiness.json"


def test_main_treats_a_non_true_known_flag_as_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `steps.<id>.outcome == 'success'` renders "false" when the step was
    # skipped or failed; anything that is not exactly "true" must not record.
    monkeypatch.setenv("CONFIGURED_KNOWN", "false")
    monkeypatch.setenv("E2E_RESULT", "skipped")
    assert build_scorecard_args.main([]) == 0
    assert "--cross-cloud-configured" not in capsys.readouterr().out
