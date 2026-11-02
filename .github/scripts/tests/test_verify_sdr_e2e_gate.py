"""Tests for .github/scripts/verify_sdr_e2e_gate.py (FND-4486)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import verify_sdr_e2e_gate as mod


@pytest.mark.parametrize("connector", ["success", "skipped", "failure"])
def test_sdr_success_passes(connector: str) -> None:
    assert mod.evaluate("success", connector) is None


def test_both_skipped_is_the_legitimate_skip() -> None:
    """No e2e request on a PR, or a docs-only merge-queue entry."""
    assert mod.evaluate("skipped", "skipped") is None


def test_sdr_skipped_while_connectors_ran_fails_closed() -> None:
    error = mod.evaluate("skipped", "success")
    assert error is not None and "failing the gate closed" in error


@pytest.mark.parametrize("sdr", ["failure", "cancelled", ""])
@pytest.mark.parametrize("connector", ["success", "skipped"])
def test_a_failed_or_missing_sdr_leg_fails(sdr: str, connector: str) -> None:
    """The PR #4638 shape: SDR red while every connector check was green."""
    error = mod.evaluate(sdr, connector)
    assert error is not None and "did not succeed" in error


def test_cli_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert mod.main(["--sdr-result", "success", "--connector-result", "success"]) == 0
    assert mod.main(["--sdr-result", "failure", "--connector-result", "success"]) == 1
    assert "::error::" in capsys.readouterr().out


def test_cli_requires_both_results() -> None:
    with pytest.raises(SystemExit):
        mod.main(["--sdr-result", "success"])


# ── wiring ─────────────────────────────────────────────────────────────────

import itertools  # noqa: E402
from typing import Any  # noqa: E402

import yaml  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gha_expr import evaluate  # noqa: E402

WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/pull_request.yaml"


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _if(job: str) -> str:
    source = str(_jobs()[job]["if"]).strip()
    if source.startswith("${{") and source.endswith("}}"):
        source = source[3:-2].strip()
    return source


def test_connector_gate_needs_and_checks_the_sdr_leg() -> None:
    gate = _jobs()["connector-gate"]
    assert "sdr-k8s-e2e" in gate["needs"]
    (step,) = [s for s in gate["steps"] if s.get("name") == "Require SDR K8s E2E"]
    # Runs even when the connector wait failed, so both verdicts are reported.
    assert step["if"].replace(" ", "").startswith("always()&&")
    assert "verify_sdr_e2e_gate.py" in step["run"]
    assert step["env"]["SDR_RESULT"] == "${{ needs.sdr-k8s-e2e.result }}"
    assert step["env"]["CONNECTOR_RESULT"] == "${{ needs.connector-tests.result }}"


@pytest.mark.parametrize(
    ("event", "sdk", "requested", "fork", "author"),
    list(
        itertools.product(
            ("pull_request", "merge_group"),
            ("true", "false"),
            ("true", "false"),
            (False, True),
            ("a-human", "dependabot[bot]"),
        )
    ),
)
def test_sdr_and_connector_legs_share_a_run_condition(
    event: str, sdk: str, requested: str, fork: bool, author: str
) -> None:
    """The premise of "skipped is fine only when connector-tests skipped":
    with a healthy matrix and base image, both legs run on the same events."""
    ctx = {
        "github": {
            "event_name": event,
            "event": {
                "pull_request": {
                    "head": {"repo": {"fork": fork}},
                    "user": {"login": author},
                }
            },
        },
        "needs": {
            "changes": {
                "result": "success",
                "outputs": {"sdk": sdk, "container": "false", "ci": "false"},
            },
            "e2e-request": {"outputs": {"requested": requested}},
            "matrix-builder": {"result": "success"},
            "build-sdk-base-image": {"result": "skipped"},
            "merge-sdk-base-image": {"result": "skipped"},
        },
    }
    assert evaluate(_if("sdr-k8s-e2e"), ctx) == evaluate(_if("connector-tests"), ctx)
