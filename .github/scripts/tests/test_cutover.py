"""The loop lane runs on its own artefacts now.

The cutover's whole risk is a leftover: one pointer into the old corpus, one
constant still naming the old router, and the lane quietly goes back to
spending its opening turns orienting. Nothing goes red when that happens — the
review just costs more and nobody can see why — so the invariants are asserted
here instead.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sdk_loop_phase  # noqa: E402
from sdk_loop_common import PLAYBOOK_REVIEW  # noqa: E402
from sdk_loop_phase import DismissalLedger, review_prompt  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[3]


def _prompt(**kw) -> str:
    return review_prompt(1, 1, "a" * 40, DismissalLedger(), **kw)


def test_the_lane_reads_its_own_playbook() -> None:
    assert PLAYBOOK_REVIEW == ".mothership/pr-loop/REVIEW.md"
    assert (REPO / PLAYBOOK_REVIEW).exists()


def test_the_playbook_arrives_in_the_prompt() -> None:
    """ "Read <playbook> and follow it exactly" bought eight measured
    orientation turns before the diff was touched. The playbook is 8.1 KB; it
    travels with the prompt."""
    prompt = _prompt(scope="full", agents=("correctness",))
    assert "# SDK reviewer" in prompt
    assert "What counts as a finding" in prompt


def test_the_prompt_points_at_nothing_in_the_other_lane() -> None:
    """One surviving pointer buys the whole orientation sequence back, and the
    sandbox corpus still exists — so this cannot be enforced by deleting it."""
    for kw in (
        {"scope": "full", "agents": ("correctness", "quality")},
        {"scope": "config-only", "solo": "ci-config"},
    ):
        assert ".mothership/pr-review/" not in _prompt(**kw)


def test_a_solo_specialist_gets_its_brief_inline() -> None:
    """A reviewer that had to open a file to learn its own domain would spend a
    turn doing it — the exact cost the cutover exists to remove."""
    assert "# CI and configuration" in _prompt(scope="config-only", solo="ci-config")


def test_the_pack_is_injected_when_one_is_built() -> None:
    packed = _prompt(
        scope="full", agents=("correctness",), pack="## Files in this change"
    )
    assert "## Files in this change" in packed


def test_a_missing_playbook_degrades_rather_than_aborts(monkeypatch) -> None:
    """A round that cannot read its playbook should return a partial review that
    says so, not crash the phase — and certainly not review confidently with no
    instructions at all."""
    monkeypatch.setattr(sdk_loop_phase, "_read", lambda path: "")
    prompt = _prompt(scope="full", agents=("correctness",))
    assert "WARNING" in prompt
    assert "partial" in prompt


def test_the_stage_documents_exist_for_the_cutover_to_reach() -> None:
    """REFUTE.md and HYPOTHESES.md are stages the runner drives. A missing one
    is a stage that silently never runs."""
    for name in ("REVIEW.md", "REFUTE.md", "HYPOTHESES.md"):
        assert (REPO / ".mothership/pr-loop" / name).exists(), f"{name} is missing"
