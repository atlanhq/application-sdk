"""Tests for the M-series (customer-facing metadata) model-driven checks.

A typed stub client (no live model) drives M001/M002 verdicts.  Each test writes
a minimal ``atlan.yaml`` under ``tmp_path`` and calls the checker's
:func:`scan_all`.  Also covers offline clean-skip, allowlist suppression, the
SARIF projection (mechanism/provenance/evidence), and the registry invariant.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import TypeVar

import pytest
from conformance.suite.checks import metadata
from conformance.suite.checks._model_common import API_KEY_ENV, set_client_override
from conformance.suite.checks.metadata._surfaces import iter_surfaces
from conformance.suite.rules import CATALOG, get_rule
from conformance.suite.schema.disposition import (
    Disposition,
    EnforcementTier,
    RuleMechanism,
    derive_disposition,
)
from conformance.suite.schema.findings import findings_to_report
from pydantic import BaseModel

_T = TypeVar("_T", bound=BaseModel)

_DIRTY_YAML = dedent(
    """\
    name: amazon-redshift-miner
    display_name: Amazon Redshift Miner
    short_description: Mines Redshift metadata for the FOOBAR-123 pipeline.
    long_description: |-
      Extracts schemas and tables from the warehouse.
      Internal note: ping the data-plane oncall before running.
    type: connector
    entrypoints:
    - name: amazon-redshift-miner
      display_name: Amazon Redshift Miner
      description: Runs the FOOBAR-123 extraction.
    """
)

_CLEAN_YAML = dedent(
    """\
    name: redshift-miner
    display_name: Redshift Miner
    short_description: Extracts metadata from Redshift databases.
    long_description: |-
      Extracts schemas, tables, and columns from a Redshift cluster
      and publishes them to the catalog.
    type: connector
    entrypoints:
    - name: redshift-miner
      display_name: Redshift Miner
      description: Runs the Redshift metadata extraction.
    """
)


class _StubClient:
    """Flags any classified text containing one of the given sentinels."""

    def __init__(self, flags: frozenset[str]) -> None:
        self._flags = flags

    def classify(self, *, system: str, user: str, schema: type[_T]) -> _T:
        low = user.lower()
        for sentinel in self._flags:
            if sentinel.lower() in low:
                return schema(
                    flagged=True, evidence=sentinel, explanation="stub reason"
                )
        return schema(flagged=False)


_SENTINELS = frozenset({"amazon", "FOOBAR", "Internal note"})


@pytest.fixture(autouse=True)
def _reset_override():
    yield
    set_client_override(None)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "atlan.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Surface extraction
# ---------------------------------------------------------------------------


def test_iter_surfaces_extracts_names_and_descriptions() -> None:
    surfaces = iter_surfaces(_DIRTY_YAML)
    by_field = {(s.field, s.location): s for s in surfaces}
    # Top-level name + display_name + short/long description present.
    assert ("name", "top-level") in by_field
    assert ("short_description", "top-level") in by_field
    # Block scalar joined and dedented.
    long_desc = by_field[("long_description", "top-level")].value
    assert "Internal note" in long_desc
    # Entrypoint description captured with its location label.
    ep = by_field[("description", "entrypoint 'amazon-redshift-miner'")]
    assert "FOOBAR-123" in ep.value


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_scan_all_flags_naming_and_jargon(tmp_path: Path) -> None:
    set_client_override(_StubClient(_SENTINELS))
    findings = metadata.scan_all([_write(tmp_path, _DIRTY_YAML)], tmp_path)
    ids = {f.rule_id for f in findings}
    assert ids == {"M001", "M002"}
    # Every finding anchors to atlan.yaml and carries the flagged evidence span.
    assert all(f.file == "atlan.yaml" for f in findings)
    assert all(f.evidence for f in findings)
    m002 = [f for f in findings if f.rule_id == "M002"]
    assert any("FOOBAR" in (f.evidence or "") for f in m002)


def test_scan_all_clean_copy_no_findings(tmp_path: Path) -> None:
    set_client_override(_StubClient(_SENTINELS))
    findings = metadata.scan_all([_write(tmp_path, _CLEAN_YAML)], tmp_path)
    assert findings == []


def test_scan_all_offline_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    set_client_override(None)
    findings = metadata.scan_all([_write(tmp_path, _DIRTY_YAML)], tmp_path)
    assert findings == []


def test_allowlist_directive_suppresses(tmp_path: Path) -> None:
    set_client_override(_StubClient(_SENTINELS))
    yaml = _DIRTY_YAML.replace(
        "short_description: Mines Redshift metadata for the FOOBAR-123 pipeline.",
        "short_description: Mines Redshift metadata for the FOOBAR-123 pipeline.  # conformance: ignore[M002] reviewed and accepted",
    )
    findings = metadata.scan_all([_write(tmp_path, yaml)], tmp_path)
    short_desc = [f for f in findings if f.rule_id == "M002" and f.line == 3]
    assert short_desc, "expected a finding on the short_description line"
    assert all(f.suppressed for f in short_desc)


# ---------------------------------------------------------------------------
# SARIF projection
# ---------------------------------------------------------------------------


def test_sarif_shape_marks_model_mechanism_and_evidence(tmp_path: Path) -> None:
    set_client_override(_StubClient(_SENTINELS))
    findings = metadata.scan_all([_write(tmp_path, _DIRTY_YAML)], tmp_path)
    report = findings_to_report(findings, tool_version="test")
    run = report.runs[0]

    # Rule descriptor carries mechanism + provenance.
    rules_by_id = {r.id: r for r in run.tool.driver.rules}
    m002_props = rules_by_id["M002"].properties
    assert m002_props["atlan/mechanism"] == "model"
    assert m002_props["atlan/modelId"]
    assert m002_props["atlan/promptVersion"] == "m002-v1"

    # Results are advisory warnings and carry the evidence span.
    model_results = [r for r in run.results if r.rule_id in {"M001", "M002"}]
    assert model_results
    for r in model_results:
        assert r.level == "warning"
        assert derive_disposition(r) in {Disposition.WARNING, Disposition.SUPPRESSED}
        assert r.properties.get("atlan/evidence")


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


def test_m_series_registered_as_advisory_model_rules() -> None:
    m_rules = [r for rid, r in CATALOG.items() if rid[0] == "M"]
    assert {r.id for r in m_rules} == {"M001", "M002"}
    for rule in m_rules:
        assert rule.mechanism is RuleMechanism.MODEL
        # This first cut ships all model rules advisory (never blocking).
        assert rule.tier is EnforcementTier.WARN
        assert rule.model_id and rule.prompt_version


def test_no_model_rule_blocks() -> None:
    for rule in CATALOG.values():
        if rule.mechanism is RuleMechanism.MODEL:
            assert rule.tier is EnforcementTier.WARN
    # Sanity: at least the M-series exists so this test is meaningful.
    assert any(r.mechanism is RuleMechanism.MODEL for r in CATALOG.values())
    _ = get_rule("M001")
