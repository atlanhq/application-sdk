"""End-to-end tests for the ``scorecard`` CLI subcommand (per-tier flags)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.scorecard.cli import _app_from_repo, main
from conformance.scorecard.schema import Scorecard

_FIXTURES = Path(__file__).parent / "fixtures" / "scorecard"


def _junit(tmp_path: Path, name: str, *, passed: int = 0, failed: int = 0) -> Path:
    cases = "".join(
        f'<testcase classname="t.{name}" name="p{i}" time="0.1"/>'
        for i in range(passed)
    ) + "".join(
        f'<testcase classname="t.{name}" name="f{i}" time="0.1"><failure/></testcase>'
        for i in range(failed)
    )
    total = passed + failed
    p = tmp_path / f"{name}.xml"
    p.write_text(
        f'<testsuites><testsuite name="pytest" tests="{total}">{cases}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return p


def test_cli_per_tier_writes_valid_scorecard(tmp_path: Path) -> None:
    out = tmp_path / "results" / "test-readiness.json"
    rc = main(
        [
            "--unit-junit",
            str(_junit(tmp_path, "unit", passed=40)),
            "--unit-coverage",
            str(_FIXTURES / "coverage.json"),
            "--integration-junit",
            str(_junit(tmp_path, "integration", passed=5)),
            "--integration-coverage",
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
    sc = Scorecard.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert sc.repo == "atlanhq/atlan-mysql-app"
    assert sc.app == "mysql"
    assert sc.commit_sha == "deadbeef"
    # e2e junit not supplied → e2e tier not applicable, no B cap
    assert next(t for t in sc.tiers if t.name == "e2e").applicable is False
    assert next(g for g in sc.gates if g.id == "e2e-present").status == "na"
    # per-tier coverage recorded
    assert set(sc.raw.coverage.keys()) == {"unit", "integration"}


def test_cli_with_e2e_junit_marks_e2e_applicable(tmp_path: Path) -> None:
    out = tmp_path / "sc.json"
    rc = main(
        [
            "--unit-junit",
            str(_junit(tmp_path, "unit", passed=10)),
            "--integration-junit",
            str(_junit(tmp_path, "integration", passed=3)),
            "--e2e-junit",
            str(_junit(tmp_path, "e2e", passed=1)),
            "--repo",
            "atlanhq/atlan-openapi-app",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    sc = Scorecard.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert next(t for t in sc.tiers if t.name == "e2e").applicable is True
    assert next(g for g in sc.gates if g.id == "e2e-present").status == "pass"


def test_cli_requires_unit_junit(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--repo", "atlanhq/atlan-x-app", "--out", str(tmp_path / "x.json")])


def test_cli_legacy_junit_alias_maps_to_unit(tmp_path: Path) -> None:
    out = tmp_path / "sc.json"
    rc = main(
        [
            "--junit",
            str(_junit(tmp_path, "unit", passed=10)),
            "--coverage",
            str(_FIXTURES / "coverage.json"),
            "--repo",
            "atlanhq/atlan-mysql-app",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    sc = Scorecard.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert "unit" in sc.raw.coverage


def test_app_from_repo() -> None:
    assert _app_from_repo("atlanhq/atlan-mysql-app") == "mysql"
    assert _app_from_repo("atlanhq/atlan-hello-world-app") == "hello-world"
    assert _app_from_repo("atlanhq/some-other-repo") == "some-other-repo"
