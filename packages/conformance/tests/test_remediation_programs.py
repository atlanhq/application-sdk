"""Wiring tests for the shipped OpenProse remediation programs.

The programs are executed by an agent, not by an interpreter, so nothing else in
CI notices when a parameter is added to a contract's signature but not threaded
through its callers. The failure is silent and expensive: a run scoped to one rule
that quietly widens to a whole series, or a suggest-only area that starts applying
because a default went missing.

These tests read the shipped contracts as text and assert the threading holds.
They deliberately assert on the *call sites*, not on prose wording, so
rewording a contract does not break them but dropping an argument does.
"""

from __future__ import annotations

import re
from importlib.resources import files

import pytest

PROGRAMS = files("conformance").joinpath("programs")

# Areas whose default is propose-don't-apply, and which therefore take
# `apply_unverifiable`. Kept explicit rather than derived: the point of the test
# is to notice if a fourth area quietly joins them.
SUGGEST_ONLY_AREAS = ("prescriptions-area", "dockerfile-area", "security-area")


def _read(rel: str) -> str:
    return PROGRAMS.joinpath(rel).read_text()


@pytest.fixture(scope="module")
def top_level() -> str:
    return _read("conformance-remediation.prose.md")


@pytest.fixture(scope="module")
def loop() -> str:
    return _read("patterns/detect-fix-recheck.prose.md")


# ── rule_ids threading ────────────────────────────────────────────────────


def test_detect_violations_documents_rule_ids_as_a_post_filter() -> None:
    """The contract states rule scoping is a post-filter, and why.

    A future reader who assumes `--series L004` works will produce an empty report
    and conclude the rule is clean. The contract has to say so.
    """
    text = _read("functions/detect-violations.prose.md")
    assert "`rule_ids`" in text
    assert "post-filter" in text
    assert "result.rule_id" in text
    # The specific trap: --series matches a series LETTER, so --series L004
    # activates zero checks rather than filtering to L004.
    assert "--series L004" in text or "--series <rule>" in text


def test_every_detect_call_in_the_loop_threads_rule_ids(loop: str) -> None:
    """Both detect-violations calls in the loop forward rule_ids.

    The end-of-round re-detect matters as much as the first call: if it widens to
    the whole series, the loop compares a one-rule fingerprint set against a
    whole-series one, never converges, and escalates a rule it actually fixed.
    """
    calls = re.findall(r"call detect-violations\n((?:\s{2,}\w+:.*\n)+)", loop)
    assert len(calls) == 2, f"expected 2 detect-violations calls, found {len(calls)}"
    for i, body in enumerate(calls):
        assert "rule_ids: rule_ids" in body, (
            f"detect-violations call #{i + 1} in detect-fix-recheck.prose.md does "
            "not forward rule_ids"
        )


def test_top_level_threads_rule_ids_to_every_area(top_level: str) -> None:
    """Every area call forwards rule_ids — a missed one silently ignores --rule."""
    area_calls = re.findall(r"call ([a-z\-]+-area)\n((?:\s{4}\w+:.*\n)+)", top_level)
    assert len(area_calls) >= 11, f"expected >=11 area calls, found {len(area_calls)}"
    missing = [name for name, body in area_calls if "rule_ids: rule_ids" not in body]
    assert not missing, f"area call(s) not forwarding rule_ids: {missing}"


# ── apply_unverifiable threading ──────────────────────────────────────────


def test_apply_unverifiable_goes_to_exactly_the_suggest_only_areas(
    top_level: str,
) -> None:
    """Only P/I/S take the flag.

    Threading it into an area that already applies would be meaningless; missing it
    on one of these three makes `--apply-unverifiable` a partial no-op that looks
    like it worked.
    """
    area_calls = re.findall(r"call ([a-z\-]+-area)\n((?:\s{4}\w+:.*\n)+)", top_level)
    got = {
        name
        for name, body in area_calls
        if "apply_unverifiable: apply_unverifiable" in body
    }
    assert got == set(SUGGEST_ONLY_AREAS), (
        f"apply_unverifiable threaded to {sorted(got)}, expected "
        f"{sorted(SUGGEST_ONLY_AREAS)}"
    )


@pytest.mark.parametrize("area", ["prescriptions", "dockerfile", "security"])
def test_suggest_only_area_declares_the_flag_and_keeps_a_default_path(
    area: str,
) -> None:
    """Each area documents the requirement, defaults false, and keeps both branches.

    Defaulting false is the compatibility guarantee: a caller that does not opt in
    must get byte-identical behaviour to before the flag existed.
    """
    text = _read(f"areas/{area}.prose.md")
    assert "`apply_unverifiable`" in text, f"{area} does not declare apply_unverifiable"
    assert "default `false`" in text, f"{area} does not state the false default"
    assert "if apply_unverifiable:" in text, f"{area} has no opt-in branch"
    assert "\nelse:" in text, f"{area} lost its propose-only branch"


@pytest.mark.parametrize("area", ["prescriptions", "security"])
def test_blind_gate_areas_force_the_unverifiable_classification(area: str) -> None:
    """P and S must not be able to emit a result that reads as gate-verified.

    Their gates pass on any edit, so the classification is the only thing standing
    between "applied" and "verified". The dockerfile area is deliberately excluded:
    its docker-build gate is real, so its fixes are genuinely verified.
    """
    text = _read(f"areas/{area}.prose.md")
    assert 'classification_override: "unverifiable"' in text
    assert "require_cited_evidence: true" in text


def test_dockerfile_area_does_not_claim_to_be_unverifiable() -> None:
    """I-series is gated for real, so it must not force the unverifiable label."""
    text = _read("areas/dockerfile.prose.md")
    assert "classification_override" not in text
    assert "docker-build" in text


def test_security_area_delivers_as_draft() -> None:
    """A credential relocation cannot merge on a green check alone."""
    assert "deliver_as_draft: true" in _read("areas/security.prose.md")


def test_security_area_forbids_moving_the_value() -> None:
    """The area states the value/reference boundary explicitly."""
    text = _read("areas/security.prose.md")
    assert "Never move a secret value" in text


# ── loop honours the new parameters ───────────────────────────────────────


def test_loop_declares_all_new_parameters(loop: str) -> None:
    for param in (
        "`rule_ids`",
        "`classification_override`",
        "`require_cited_evidence`",
        "`deliver_as_draft`",
    ):
        assert param in loop, f"detect-fix-recheck.prose.md does not declare {param}"


def test_uncited_fix_is_rejected_before_it_is_applied(loop: str) -> None:
    """The evidence check precedes `apply result.edit`.

    Checking after applying would leave a guessed value on disk between the write
    and the revert, and would make the revert path load-bearing for correctness
    rather than for cleanup.
    """
    evidence_at = loop.index("require_cited_evidence and")
    apply_at = loop.index("apply result.edit")
    assert (
        evidence_at < apply_at
    ), "the cited-evidence check must run before the edit is applied"


def test_unverifiable_always_routes_to_residue(loop: str) -> None:
    """`unverifiable` is in the residue condition alongside `judgment`."""
    assert 'result.classification == "unverifiable"' in loop


# ── docker-build gate ─────────────────────────────────────────────────────


def test_docker_build_gate_never_passes_when_docker_is_absent() -> None:
    """A gate that cannot run must not report success.

    This is the whole reason the I-series can be allowed to apply: if the gate
    silently passed on a machine without a daemon, `--apply-unverifiable` would be
    accepting Dockerfile edits on trust, which is exactly the state the area was
    written to avoid.
    """
    text = _read("functions/docker-build-gate.prose.md")
    assert "docker_absent" in text
    assert "`passed` is always false" in text
    # `which docker` is not sufficient: a client with no daemon would pass it.
    assert "docker info" in text


def test_docker_build_gate_cleans_up_its_image() -> None:
    """A long sweep must not accumulate one image per remediated rule."""
    text = _read("functions/docker-build-gate.prose.md")
    assert "docker image rm" in text
