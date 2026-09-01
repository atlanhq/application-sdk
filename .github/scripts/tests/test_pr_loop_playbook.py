"""Invariants of the `@sdk-loop` playbook — the ones that fail silently.

`.mothership/pr-loop/REVIEW.md` is injected into the reviewer's context on every
round, so a regression here is paid on every review and shows up as nothing more
than a slower, worse review. That is the same failure shape the pr-review
playbook accumulated `test_review_playbook_sections.py` to guard against, for
the same reason.

The specific regressions:

- The router grows back. The file this replaces reached 51 KB (83 KB on `main`)
  by accretion, one reasonable-looking paragraph at a time.
- Someone re-adds a "read these first" list, and the reviewer is back to eight
  orientation turns loading a corpus the runner already applied.
- The severity vocabulary drifts from the data the runner clamps against — the
  exact two-files-one-string drift that produced three spellings and a fourth
  nobody mapped.
"""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
PLAYBOOK = ROOT / ".mothership/pr-loop/REVIEW.md"
CONTRACT = ROOT / ".mothership/pr-loop/CONTRACT.md"
SEVERITY = ROOT / ".mothership/pr-loop/data/severity.yaml"

#: 1 token ~= 4 chars. The point of the redesign is that this file is small
#: enough to be free; the number is a ratchet, not a target to grow into.
CHARS_PER_TOKEN = 4
PLAYBOOK_TOKEN_CEILING = 2_600


def test_the_playbook_stays_small() -> None:
    text = PLAYBOOK.read_text(encoding="utf-8")
    tokens = len(text) // CHARS_PER_TOKEN
    assert tokens <= PLAYBOOK_TOKEN_CEILING, (
        f"REVIEW.md is ~{tokens} tokens, over the {PLAYBOOK_TOKEN_CEILING} "
        "ceiling. It is injected on every round of every review. The file it "
        "replaces got to 83 KB one reasonable paragraph at a time — if this "
        "content genuinely belongs, move something else out first."
    )


def test_the_playbook_never_sends_the_reviewer_to_the_old_corpus() -> None:
    """Structural, because prose prohibition demonstrably does not work.

    The pr-review playbook delists `review-policy.md` and `review.yaml` in so
    many words, and the reviewer read both in every transcript sampled. The
    only reliable fix is not naming them — so this asserts the new playbook
    does not.
    """
    text = PLAYBOOK.read_text(encoding="utf-8")
    for forbidden in (
        "ORCHESTRATION.md",
        "severity-rubric.yaml",
        "retro-log.md",
        "review-policy.md",
        "review.yaml",
        "references/",
        "pr-review/",
    ):
        assert forbidden not in text, (
            f"REVIEW.md points the reviewer at {forbidden}. Everything in the "
            "old corpus is either applied by the runner or in the pack; a "
            "pointer here buys turns and nothing else."
        )


def test_the_playbook_does_not_point_at_the_maintainer_doc() -> None:
    """`CONTRACT.md` is maintainer-facing and must stay out of agent context."""
    assert "CONTRACT.md" not in PLAYBOOK.read_text(encoding="utf-8")
    assert "No agent reads this file" in CONTRACT.read_text(encoding="utf-8")


def test_the_emitted_vocabulary_matches_the_runner_data() -> None:
    """One vocabulary, defined once.

    The corpus this forked from carried three spellings with no mapping, and a
    fourth (`IMPORTANT`) that belonged to none of them. The playbook tells the
    reviewer what to emit; `severity.yaml` tells the runner what to accept. If
    they disagree, every finding of the mismatched tier fails the round.
    """
    listed = set(
        re.findall(
            r"`(BLOCKING|CRITICAL|HIGH|MEDIUM|LOW|INFO)`",
            PLAYBOOK.read_text(encoding="utf-8"),
        )
    )
    accepted = set(yaml.safe_load(SEVERITY.read_text(encoding="utf-8"))["display"])
    assert (
        listed == accepted
    ), f"playbook offers {sorted(listed)}, runner accepts {sorted(accepted)}"


def test_the_playbook_names_the_completion_assertion() -> None:
    """An empty findings list renders READY_TO_MERGE.

    So the reviewer must be told, in the file it actually reads, that
    `status` and `reviewed_files` are how a real empty result is told from a
    crash. Without them a dead agent manufactures an approval.
    """
    text = PLAYBOOK.read_text(encoding="utf-8")
    assert "reviewed_files" in text
    assert '"status": "partial"' in text
    assert "reads as an approval" in text


def test_the_playbook_keeps_the_class_sweep() -> None:
    """The single highest-leverage judgement step in the old playbook.

    Nothing deterministic replaces it: clustering by root cause and sweeping the
    diff for siblings is what holds a PR to two rounds instead of twenty. It is
    the one large block deliberately carried over nearly whole.
    """
    text = PLAYBOOK.read_text(encoding="utf-8")
    assert "Bugs travel in classes" in text
    assert "Cluster by root cause" in text
    assert "Sweep the whole diff" in text
    assert "hollow" in text, "the always-pass gate check is missing"


def test_the_playbook_keeps_nits_narrow_and_real_bugs_exempt() -> None:
    """Convergence rules apply to MEDIUM only — never to a real defect.

    Diff-scoping a Critical would suppress exactly the regressions the resolver
    introduces, which is the one thing a review loop must never do.
    """
    # Collapse wrapping — these are prose assertions and the file is hard-wrapped.
    text = " ".join(PLAYBOOK.read_text(encoding="utf-8").split())
    assert "for `MEDIUM` **only**" in text
    assert "exempt from all three" in text
    assert "**especially** code the resolver just pushed" in text


def test_the_playbook_forbids_reading_ci() -> None:
    text = PLAYBOOK.read_text(encoding="utf-8")
    assert "CI state" in text
    assert "stale" in text
