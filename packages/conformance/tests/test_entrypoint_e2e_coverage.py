"""Tests for T025 EntrypointWithoutE2ECoverage.

Two things must hold, and the second matters more than the first:

* It fires once per uncovered entrypoint on a bundle app.
* It is **silent** on every other shape — a single-entrypoint app, and a
  route/card-split app whose secondary entrypoints run as DAG nodes inside the
  parent's own run. The vast majority of the fleet is the former and
  ``atlan-metabase-app`` is the latter; a rule that lit either of them up would
  get suppressed wholesale and stop being worth having.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.entrypoint_e2e_coverage import (
    RULE_T025,
    discover,
    scan_all,
)

_MANIFEST = {
    "execution_mode": "automation-engine",
    "dag": {"extract": {"activity_name": "execute_workflow"}},
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bundle(root: Path, *entrypoints: str) -> None:
    """Lay out a bundle-mode contract: app/generated/<ep>/manifest.json each."""
    for ep in entrypoints:
        _write(
            root / "app" / "generated" / ep / "manifest.json",
            json.dumps(_MANIFEST),
        )


def _single(root: Path, *routes: str) -> None:
    """Lay out a single-mode contract, optionally with DAG-routed entrypoints."""
    dag: dict[str, object] = {
        "extract": {"inputs": {"workflow_type": "myapp:extract-metadata"}}
    }
    for i, route in enumerate(routes):
        dag[f"node{i}"] = {"inputs": {"workflow_type": f"myapp:{route}"}}
    _write(
        root / "app" / "generated" / "manifest.json",
        json.dumps({"execution_mode": "automation-engine", "dag": dag}),
    )


def _cover(root: Path, entrypoint: str) -> None:
    """Add an e2e suite covering *entrypoint*.

    Used to park a sibling as already-covered, so a test about one entrypoint's
    resolution is not also exercising the 2+-entrypoint scope gate.
    """
    stem = entrypoint.replace("-", "_")
    _write(
        root / "tests" / "e2e" / f"test_covered_{stem}.py",
        f'class TestCovered(BaseE2ETest):\n    entrypoint = "{entrypoint}"\n',
    )


def _pyproject(root: Path, body: str = "") -> None:
    _write(root / "pyproject.toml", body or '[project]\nname = "myapp"\n')


def _run(root: Path) -> list[str]:
    """Return the messages T025 reports for the repo at *root*."""
    findings = scan_all(discover(root), root)
    assert all(f.rule_id == RULE_T025 for f in findings)
    return [f.message for f in findings]


# ---------------------------------------------------------------------------
# Out of scope — the silence cases
# ---------------------------------------------------------------------------


class TestSilentOutsideBundleMode:
    def test_no_generated_tree_is_silent(self, tmp_path: Path) -> None:
        _pyproject(tmp_path)
        assert _run(tmp_path) == []

    def test_single_entrypoint_app_is_silent(self, tmp_path: Path) -> None:
        """The overwhelming majority of the fleet. The target is unambiguous."""
        _pyproject(tmp_path)
        _single(tmp_path)
        assert _run(tmp_path) == []

    def test_route_card_split_app_is_silent(self, tmp_path: Path) -> None:
        """The metabase shape: one card, secondary entrypoints as DAG nodes.

        Those run inside the parent's own full-DAG execution, so they are covered
        transitively. Flagging them would be a false positive against a
        deliberate design.
        """
        _pyproject(tmp_path)
        _single(tmp_path, "extract-lineage")
        assert _run(tmp_path) == []

    def test_a_one_entrypoint_bundle_is_left_to_t012(self, tmp_path: Path) -> None:
        """Bundle mode with a single entrypoint (the hive shape) is skipped.

        There is no asymmetry to report, and T012 already names the missing tier.
        Two findings for one missing file is noise that gets a rule suppressed.
        """
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler")
        assert _run(tmp_path) == []

    def test_e2e_tier_exemption_is_honoured(self, tmp_path: Path) -> None:
        """A repo with no e2e tier at all is not asked for per-entrypoint suites."""
        _pyproject(
            tmp_path,
            '[project]\nname = "myapp"\n\n'
            "[tool.conformance]\n"
            'exempt_test_tiers = ["e2e"]\n',
        )
        _bundle(tmp_path, "crawler", "miner")
        assert _run(tmp_path) == []


# ---------------------------------------------------------------------------
# In scope — detection
# ---------------------------------------------------------------------------


class TestBundleDetection:
    def test_reports_every_entrypoint_when_there_is_no_e2e_tier(
        self, tmp_path: Path
    ) -> None:
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")

        messages = _run(tmp_path)

        assert len(messages) == 2
        assert any("'crawler'" in m for m in messages)
        assert any("'miner'" in m for m in messages)

    def test_reports_only_the_uncovered_entrypoint(self, tmp_path: Path) -> None:
        """The exact APP-2700 shape: crawler wired, miner not."""
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _write(
            tmp_path / "tests" / "e2e" / "test_myapp_crawler_e2e.py",
            "class TestCrawler(CrawlerGeneratedE2EBase):\n    pass\n",
        )

        messages = _run(tmp_path)

        assert len(messages) == 1
        assert "'miner'" in messages[0]
        # The message names what IS covered, so the reader sees the asymmetry.
        assert "covers: crawler" in messages[0]

    def test_silent_when_every_entrypoint_is_covered(self, tmp_path: Path) -> None:
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _write(
            tmp_path / "tests" / "e2e" / "test_crawler.py",
            "class TestCrawler(CrawlerGeneratedE2EBase):\n    pass\n",
        )
        _write(
            tmp_path / "tests" / "e2e" / "test_miner.py",
            "class TestMiner(MinerGeneratedE2EBase):\n    pass\n",
        )
        assert _run(tmp_path) == []

    def test_reports_each_of_several_uncovered_entrypoints(
        self, tmp_path: Path
    ) -> None:
        """The databricks shape — four entrypoints, one wired."""
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner", "promote-marker", "promote-lineage")
        _cover(tmp_path, "crawler")

        messages = _run(tmp_path)

        assert len(messages) == 3
        for missing in ("miner", "promote-marker", "promote-lineage"):
            assert any(f"'{missing}'" in m for m in messages)


# ---------------------------------------------------------------------------
# The three ways a class can claim an entrypoint
# ---------------------------------------------------------------------------


class TestCoverageResolution:
    def test_generated_base_inheritance_counts(self, tmp_path: Path) -> None:
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _cover(tmp_path, "crawler")
        _write(
            tmp_path / "tests" / "e2e" / "test_m.py",
            "class TestMiner(MinerGeneratedE2EBase):\n    pass\n",
        )
        assert _run(tmp_path) == []

    def test_a_hyphenated_entrypoint_resolves_through_pascal_case(
        self, tmp_path: Path
    ) -> None:
        _pyproject(tmp_path)
        _bundle(tmp_path, "extract-metadata", "miner")
        _cover(tmp_path, "miner")
        _write(
            tmp_path / "tests" / "e2e" / "test_em.py",
            "class TestEm(ExtractMetadataGeneratedE2EBase):\n    pass\n",
        )
        assert _run(tmp_path) == []

    def test_explicit_entrypoint_attribute_counts(self, tmp_path: Path) -> None:
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _cover(tmp_path, "crawler")
        _write(
            tmp_path / "tests" / "e2e" / "test_m.py",
            'class TestMiner(BaseE2ETest):\n    entrypoint = "miner"\n',
        )
        assert _run(tmp_path) == []

    def test_manifest_path_counts(self, tmp_path: Path) -> None:
        """The oracle shape — a hand-pinned manifest path."""
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _cover(tmp_path, "miner")
        _write(
            tmp_path / "tests" / "e2e" / "test_c.py",
            "class TestCrawler(SQLAppE2ETest):\n"
            '    manifest_path = "app/generated/crawler/manifest.json"\n',
        )
        assert _run(tmp_path) == []

    def test_an_annotated_assignment_counts(self, tmp_path: Path) -> None:
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _cover(tmp_path, "crawler")
        _write(
            tmp_path / "tests" / "e2e" / "test_m.py",
            "class TestMiner(BaseE2ETest):\n"
            '    entrypoint: ClassVar[str] = "miner"\n',
        )
        assert _run(tmp_path) == []

    def test_a_wrong_entrypoint_value_does_not_cover(self, tmp_path: Path) -> None:
        """Declaring the crawler must not silence the miner."""
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _write(
            tmp_path / "tests" / "e2e" / "test_c.py",
            'class TestCrawler(BaseE2ETest):\n    entrypoint = "crawler"\n',
        )

        messages = _run(tmp_path)

        assert len(messages) == 1
        assert "'miner'" in messages[0]

    def test_a_non_test_class_does_not_cover(self, tmp_path: Path) -> None:
        """A shared helper base pytest never collects is not a suite."""
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _cover(tmp_path, "crawler")
        _write(
            tmp_path / "tests" / "e2e" / "test_m.py",
            'class MinerBase(BaseE2ETest):\n    entrypoint = "miner"\n',
        )

        messages = _run(tmp_path)

        assert len(messages) == 1
        assert "'miner'" in messages[0]

    def test_a_nested_suite_still_counts_as_coverage(self, tmp_path: Path) -> None:
        """Discovery is recursive on purpose.

        The CI matrix only fans out over the flat layout, but under-reporting a
        repo that HAS wired its miner up would make this rule wrong. Flat-layout
        enforcement is a separate concern with its own signal.
        """
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _cover(tmp_path, "crawler")
        _write(
            tmp_path / "tests" / "e2e" / "test_miner_local" / "test_run.py",
            'class TestMiner(BaseE2ETest):\n    entrypoint = "miner"\n',
        )
        assert _run(tmp_path) == []


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_an_unparseable_test_file_contributes_no_coverage(
        self, tmp_path: Path
    ) -> None:
        """A file the collector cannot read is not evidence of a wired entrypoint."""
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _cover(tmp_path, "crawler")
        _write(tmp_path / "tests" / "e2e" / "test_broken.py", "class Test(:\n")

        messages = _run(tmp_path)

        assert len(messages) == 1
        assert "'miner'" in messages[0]

    def test_a_non_collectable_filename_is_ignored(self, tmp_path: Path) -> None:
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _cover(tmp_path, "crawler")
        _write(
            tmp_path / "tests" / "e2e" / "helpers.py",
            'class TestMiner(BaseE2ETest):\n    entrypoint = "miner"\n',
        )

        assert len(_run(tmp_path)) == 1

    def test_a_missing_pyproject_does_not_crash(self, tmp_path: Path) -> None:
        _bundle(tmp_path, "crawler", "miner")
        findings = scan_all(discover(tmp_path), tmp_path)
        assert len(findings) == 2
        assert all(f.rule_id == RULE_T025 for f in findings)

    def test_findings_are_anchored_to_pyproject(self, tmp_path: Path) -> None:
        """The missing thing is a file, so there is no test line to point at."""
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")
        _cover(tmp_path, "crawler")

        findings = scan_all(discover(tmp_path), tmp_path)

        assert findings[0].file == "pyproject.toml"
        assert findings[0].line == 1

    def test_inline_suppression_is_honoured(self, tmp_path: Path) -> None:
        _pyproject(
            tmp_path,
            f"# conformance: ignore[{RULE_T025}] miner has no CI-reachable source\n"
            '[project]\nname = "myapp"\n',
        )
        _bundle(tmp_path, "crawler", "miner")
        _cover(tmp_path, "crawler")

        findings = scan_all(discover(tmp_path), tmp_path)

        assert findings
        assert all(f.suppressed for f in findings)

    def test_per_entrypoint_suppression_suppresses_only_the_named_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """``ignore[T025:miner]`` must leave a missing third entrypoint reported.

        All T025 findings share the pyproject.toml:1 anchor, so a rule-wide
        directive would suppress the lot; the ``:<entrypoint>`` discriminator is
        what lets one legitimate exemption coexist with real gaps elsewhere.
        """
        _pyproject(
            tmp_path,
            f"# conformance: ignore[{RULE_T025}:miner] miner has no CI-reachable source\n"
            '[project]\nname = "myapp"\n',
        )
        _bundle(tmp_path, "crawler", "miner", "promote-marker")
        _cover(tmp_path, "crawler")

        findings = scan_all(discover(tmp_path), tmp_path)

        assert len(findings) == 2
        by_ep = {f.discriminator: f for f in findings}
        assert by_ep["miner"].suppressed
        assert not by_ep["promote-marker"].suppressed

    def test_findings_carry_their_entrypoint_as_discriminator(
        self, tmp_path: Path
    ) -> None:
        """The discriminator is what keys the SARIF fingerprint per entrypoint."""
        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")

        findings = scan_all(discover(tmp_path), tmp_path)

        assert len(findings) == 2
        assert {f.discriminator for f in findings} == {"crawler", "miner"}

    def test_per_entrypoint_fingerprints_are_distinct(self, tmp_path: Path) -> None:
        """Two T025 findings at the same anchor must not share a fingerprint."""
        from conformance.suite.schema.findings import findings_to_report

        _pyproject(tmp_path)
        _bundle(tmp_path, "crawler", "miner")

        findings = scan_all(discover(tmp_path), tmp_path)
        report = findings_to_report(findings, tool_version="0.0.0-test")
        results = report.runs[0].results

        assert len(results) == 2
        fps = [r.partial_fingerprints["atlanConformance/v1"] for r in results]
        assert fps[0] != fps[1]
