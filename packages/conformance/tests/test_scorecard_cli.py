"""End-to-end tests for the ``scorecard`` CLI subcommand."""

from __future__ import annotations

import json
from pathlib import Path

from conformance.scorecard.cli import _app_from_repo, main
from conformance.scorecard.schema import Scorecard

_FIXTURES = Path(__file__).parent / "fixtures" / "scorecard"


def test_cli_writes_valid_scorecard(tmp_path: Path) -> None:
    out = tmp_path / "results" / "test-readiness.json"
    rc = main(
        [
            "--junit",
            str(_FIXTURES / "junit_mixed.xml"),
            "--coverage",
            str(_FIXTURES / "coverage.json"),
            "--repo",
            "atlanhq/atlan-mysql-app",
            "--commit",
            "deadbeef",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    assert out.exists()

    sc = Scorecard.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert sc.repo == "atlanhq/atlan-mysql-app"
    assert sc.app == "mysql"  # derived from repo
    assert sc.commit_sha == "deadbeef"
    # unit has a failing test → all-green gate fails → maturity cannot be earned
    assert sc.aggregate.maturity == "none"
    assert any(g.id == "all-green" and g.status == "fail" for g in sc.gates)


def test_cli_without_coverage_still_succeeds(tmp_path: Path) -> None:
    out = tmp_path / "sc.json"
    rc = main(
        [
            "--junit",
            str(_FIXTURES / "junit_mixed.xml"),
            "--repo",
            "atlanhq/atlan-openapi-app",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    sc = Scorecard.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert sc.raw.coverage is None


def test_app_from_repo() -> None:
    assert _app_from_repo("atlanhq/atlan-mysql-app") == "mysql"
    assert _app_from_repo("atlanhq/atlan-hello-world-app") == "hello-world"
    assert _app_from_repo("atlanhq/some-other-repo") == "some-other-repo"
