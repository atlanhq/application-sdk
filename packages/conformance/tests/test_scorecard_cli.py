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


def _run(tmp_path: Path, out: Path, *extra: str) -> Scorecard:
    rc = main(
        [
            "--unit-junit",
            str(_junit(tmp_path, "unit", passed=10)),
            "--integration-junit",
            str(_junit(tmp_path, "integration", passed=3)),
            "--repo",
            "atlanhq/atlan-openapi-app",
            "--out",
            str(out),
            *extra,
        ]
    )
    assert rc == 0
    return Scorecard.model_validate(json.loads(out.read_text(encoding="utf-8")))


def _leg_junit(root: Path, leg: str, *, passed: int = 0, failed: int = 0) -> None:
    """Write one leg's artifact at the layout `download-artifact` produces."""
    leg_dir = root / f"sdr-integration-tests-openapi-{leg}-results" / "results"
    leg_dir.mkdir(parents=True)
    cases = "".join(
        f'<testcase classname="tests.e2e.test_s" name="p{i}" time="1"/>'
        for i in range(passed)
    ) + "".join(
        f'<testcase classname="tests.e2e.test_s" name="f{i}" time="1"><failure/>'
        "</testcase>"
        for i in range(failed)
    )
    (leg_dir / "sdr-test-results.xml").write_text(
        f"<testsuites><testsuite name='pytest'>{cases}</testsuite></testsuites>",
        encoding="utf-8",
    )


def test_cli_e2e_glob_merges_every_leg(tmp_path: Path) -> None:
    """The FND-33 wiring: one glob, N per-leg artifacts, one e2e tier."""
    evidence = tmp_path / "e2e-evidence"
    _leg_junit(evidence, "suite-aws", passed=2)
    _leg_junit(evidence, "suite-azure", passed=1, failed=1)

    sc = _run(
        tmp_path,
        tmp_path / "sc.json",
        "--e2e-junit",
        str(evidence / "*" / "results" / "sdr-test-results.xml"),
    )
    assert next(t for t in sc.tiers if t.name == "e2e").applicable is True
    # Worst-case per test: p0/p1 pass everywhere, f0 fails on azure. Two legs of
    # two tests are TWO distinct tests, not four.
    assert sc.raw.tests.e2e.total == 3
    assert sc.raw.tests.e2e.failed == 1


def test_cli_e2e_glob_matching_nothing_leaves_the_tier_not_applicable(
    tmp_path: Path,
) -> None:
    """e2e skipped must not fabricate empty evidence and cap the grade at B."""
    sc = _run(
        tmp_path,
        tmp_path / "sc.json",
        "--e2e-junit",
        str(tmp_path / "e2e-evidence" / "*" / "results" / "sdr-test-results.xml"),
    )
    assert next(t for t in sc.tiers if t.name == "e2e").applicable is False
    assert next(g for g in sc.gates if g.id == "e2e-present").status == "na"
    assert "e2e-present" not in sc.aggregate.capped_by


def test_cli_records_cross_cloud_configured_and_observed(tmp_path: Path) -> None:
    sc = _run(
        tmp_path,
        tmp_path / "sc.json",
        "--cross-cloud-configured",
        "aws,azure,gcp",
        "--cross-cloud-observed",
        "aws",
    )
    assert sc.raw.cross_cloud is not None
    assert sc.raw.cross_cloud.configured == ["aws", "azure", "gcp"]
    assert sc.raw.cross_cloud.observed == ["aws"]


def test_cli_cross_cloud_absent_is_omitted_not_empty(tmp_path: Path) -> None:
    """Three states stay distinguishable on the wire (FND-34)."""
    out = tmp_path / "sc.json"
    sc = _run(tmp_path, out)
    assert sc.raw.cross_cloud is None
    assert "crossCloud" not in json.loads(out.read_text(encoding="utf-8"))["raw"]


def test_cli_cross_cloud_empty_configured_records_the_degraded_state(
    tmp_path: Path,
) -> None:
    """ "No tenant matrix" is a fact worth recording, not the same as unknown."""
    out = tmp_path / "sc.json"
    sc = _run(tmp_path, out, "--cross-cloud-configured", "")
    assert sc.raw.cross_cloud is not None
    assert sc.raw.cross_cloud.configured == []
    # observed stays absent — e2e told us nothing about what ran.
    assert sc.raw.cross_cloud.observed is None
    raw = json.loads(out.read_text(encoding="utf-8"))["raw"]
    assert raw["crossCloud"] == {"configured": []}


def test_cli_cross_cloud_is_never_scored(tmp_path: Path) -> None:
    """Descriptive only: recording it must not move the grade (FND-34)."""
    plain = _run(tmp_path, tmp_path / "a.json")
    with_clouds = _run(
        tmp_path, tmp_path / "b.json", "--cross-cloud-configured", "aws,azure,gcp"
    )
    assert with_clouds.aggregate.score == plain.aggregate.score
    assert with_clouds.aggregate.grade == plain.aggregate.grade
    assert [g.id for g in with_clouds.gates] == [g.id for g in plain.gates]


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
