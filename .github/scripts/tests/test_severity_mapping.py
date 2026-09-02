"""The severity vocabulary — one emitted set, one displayed set, a total map.

The corpus this lane forked from carries THREE spellings of severity with no
mapping between them: the payload vocabulary (`BLOCKING`..`INFO`), the
reference files' `Critical | Important | Minor`, and the verdict template's
`Critical | Important | Nit`. One brief emits a fourth (`IMPORTANT`). Because
`sdk_loop_finalize.py` keys only on whether `### Findings` is empty and never
on a tier name, a mis-spelled tier never failed anything — it just quietly
produced a finding the verdict had not counted.

These tests exist so that can never be quiet again.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sdk_loop_findings import SchemaError, load_severity  # noqa: E402

DATA = (
    pathlib.Path(__file__).resolve().parents[3]
    / ".mothership/pr-loop/data/severity.yaml"
)
RUBRIC = (
    pathlib.Path(__file__).resolve().parents[3]
    / ".mothership/pr-review/severity-rubric.yaml"
)

EMITTED = ("BLOCKING", "CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")


def test_every_emitted_severity_maps() -> None:
    """Exhaustive over the emitted vocabulary — no tier may be unhandled."""
    sev = load_severity(DATA)
    for severity in EMITTED:
        sev.tier(severity)  # must not raise
        sev.in_findings(severity)
        assert sev.floor(severity) > 0


@pytest.mark.parametrize("bogus", ["IMPORTANT", "Critical", "Nit", "", "MINOR"])
def test_an_unmapped_severity_raises(bogus: str) -> None:
    """A fourth spelling must fail loudly, not pass through.

    `agents/conformance.md` in the inherited corpus emits `"IMPORTANT"`, which
    belongs to none of the three vocabularies that corpus uses. Rendering it
    silently is how a finding ends up in the summary that the verdict never
    counted.
    """
    sev = load_severity(DATA)
    with pytest.raises(SchemaError):
        sev.tier(bogus)


def test_only_blocking_tiers_render_into_findings() -> None:
    """`### Findings` empty <=> READY_TO_MERGE, so Findings membership blocks.

    LOW and INFO are prose-only on purpose: the resolve loop fixes until the
    list is empty, so a tier that can never be actioned would wedge it forever.
    This also matches `calibration.severity_meanings`, which already calls
    MEDIUM/LOW/INFO "summary body only".
    """
    sev = load_severity(DATA)
    assert [s for s in EMITTED if sev.in_findings(s)] == [
        "BLOCKING",
        "CRITICAL",
        "HIGH",
        "MEDIUM",
    ]
    assert sev.tier("LOW") is None
    assert sev.tier("INFO") is None


def test_tiers_are_exactly_the_rendered_vocabulary() -> None:
    sev = load_severity(DATA)
    rendered = {sev.tier(s) for s in EMITTED if sev.in_findings(s)}
    assert rendered == set(sev.tier_order) == {"Critical", "Important", "Nit"}


def test_confidence_floors_are_per_severity_not_flat() -> None:
    """The regression guarded here is a brief re-introducing a flat floor.

    Six pr-review briefs restate the floor as a flat `>= 0.80` and one as a
    flat `0.85`, against `severity-rubric.yaml`'s own "do not restate these".
    The harm is suppression: a flat 0.80 discards every valid MEDIUM (floor
    0.55) and LOW (0.40); a flat 0.85 also discards every valid HIGH (0.80).
    """
    sev = load_severity(DATA)
    assert sev.floor("BLOCKING") == 0.85
    assert sev.floor("HIGH") == 0.80
    assert sev.floor("MEDIUM") == 0.55
    assert sev.floor("LOW") == 0.40
    assert len({sev.floor(s) for s in EMITTED}) > 1, "floors collapsed to a flat value"


def test_every_guardrail_names_a_real_pattern() -> None:
    """A guardrail keyed on a pattern_id that does not exist never fires.

    Silent by construction — the verdict simply comes back one tier softer than
    it should, and nothing logs that a guardrail was skipped.
    """
    sev = load_severity(DATA)
    for gid, entry in sev.guardrails.items():
        unknown = [p for p in entry["patterns"] if p not in sev.patterns]
        assert not unknown, f"{gid} names patterns that do not exist: {unknown}"


def test_the_shared_blocks_still_match_pr_reviews_rubric() -> None:
    """`severity.yaml` is canonical; pr-review's copy is generated from it.

    `categories` and `calibration` must stay semantically identical or the
    generator cannot reproduce `.mothership/pr-review/severity-rubric.yaml`,
    and the two lanes start disagreeing about what a pattern means. The
    loop-only additions (`display`, `guardrails`, `tier_order`) are what the
    runner needs and no model ever sees.
    """
    canonical = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    published = yaml.safe_load(RUBRIC.read_text(encoding="utf-8"))
    assert canonical["categories"] == published["categories"]
    assert canonical["calibration"] == published["calibration"]
    assert set(canonical) - set(published) == {"display", "guardrails", "tier_order"}
