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
# is to notice if a fifth area quietly joins them.
SUGGEST_ONLY_AREAS = (
    "prescriptions-area",
    "preflight-area",
    "dockerfile-area",
    "security-area",
)


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


def _call_blocks(text: str, callee: str) -> list[str]:
    """Every `call <callee>` block in a prose file, as its parameter lines.

    Structural, not a single regex over the whole file: a block ends where the
    parameter indentation does, so an assertion against one block can't be
    satisfied by a parameter that actually belongs to a different call — which
    is exactly how the original tests passed while the area→loop hop dropped
    `rule_ids`.
    """
    blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == f"call {callee}" or stripped.endswith(f"= call {callee}"):
            indent = len(lines[i]) - len(lines[i].lstrip())
            j = i + 1
            body: list[str] = []
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip():
                    break
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if nxt_indent <= indent or nxt.lstrip().startswith("#"):
                    if nxt.lstrip().startswith("#"):
                        j += 1
                        continue
                    break
                body.append(nxt)
                j += 1
            blocks.append("\n".join(body))
            i = j
        else:
            i += 1
    return blocks


ALL_AREAS = [
    "ci",
    "contract-toolkit",
    "dependency",
    "deprecation",
    "dockerfile",
    "error-handling",
    "logging",
    "optimizations",
    "preflight",
    "prescriptions",
    "security",
    "tests",
]


def test_every_catalog_series_has_a_remediation_area() -> None:
    """A series with rules but no area drops out of /remediate silently.

    The dispatcher only calls the areas it names and nothing else notices, so
    the findings are never remediated — indistinguishable from a clean repo.
    """
    from conformance.suite.rules import CATALOG

    series = {rule_id[0] for rule_id in CATALOG}
    covered: set[str] = set()
    for area in ALL_AREAS:
        covered.update(
            re.findall(r'series: "([A-Z])"', _read(f"areas/{area}.prose.md"))
        )
    orphaned = sorted(series - covered)
    assert not orphaned, f"catalog series with no remediation area: {orphaned}"

    tagging = _read("functions/detect-violations.prose.md")
    tagged = dict(re.findall(r"`([A-Z])` → `([a-z-]+)`", tagging))
    untagged = sorted(series - set(tagged))
    assert not untagged, f"detect-violations tags no area for series: {untagged}"
    assert set(tagged.values()) <= set(ALL_AREAS), tagged

    dispatch = _read("functions/remediate-finding.prose.md")
    undispatched = [area for area in ALL_AREAS if f"| `{area}` |" not in dispatch]
    assert not undispatched, f"remediate-finding dispatches no row for: {undispatched}"


@pytest.mark.parametrize("area", ALL_AREAS)
def test_every_area_forwards_rule_ids_into_every_runner_call(area: str) -> None:
    """THE hop that was actually broken (sdk-review on this PR, F1): the
    dispatcher threaded `rule_ids` to every area and every area then dropped it,
    so `--rule L004` silently widened to the whole series. Assert the area→loop
    hop and the suggest-only detect calls — every place an area invokes the
    runner."""
    text = _read(f"areas/{area}.prose.md")
    blocks = _call_blocks(text, "detect-fix-recheck") + _call_blocks(
        text, "detect-violations"
    )
    assert blocks, f"{area} makes no runner calls — the test would be vacuous"
    missing = [b for b in blocks if "rule_ids: rule_ids" not in b]
    assert not missing, (
        f"{area}: {len(missing)} runner call(s) do not forward rule_ids — "
        f"a --rule-scoped run widens to the whole series at this hop:\n"
        + "\n---\n".join(missing)
    )


@pytest.mark.parametrize("area", ALL_AREAS)
def test_every_area_declares_rule_ids(area: str) -> None:
    assert "`rule_ids`" in _read(
        f"areas/{area}.prose.md"
    ), f"{area} forwards rule_ids but never declares it as a parameter"


def test_remediate_finding_declares_the_evidence_field() -> None:
    """`require_cited_evidence` gates on `result.evidence`; the producer contract
    must declare it or the blind-gate areas key off an unspecified model field
    (sdk-review on this PR, F2)."""
    text = _read("functions/remediate-finding.prose.md")
    assert "`evidence`" in text
    assert "require_cited_evidence" in text


@pytest.mark.parametrize("area", ["prescriptions", "preflight", "security"])
def test_blind_gate_prescriptions_point_at_result_evidence(area: str) -> None:
    assert "result.evidence" in _read(f"areas/{area}.prose.md")


def test_docker_build_gate_fails_closed_when_no_dockerfile_is_found() -> None:
    """A gate that passes when its subject goes missing is an always-pass path
    (sdk-review on this PR, F4): a fix could omit the Dockerfile from
    model-reported touched_files and sail through."""
    text = _read("functions/docker-build-gate.prose.md")
    assert "fail closed" in text
    assert "finding.file" in text
    # The old fast-pass must be gone.
    assert "nothing to build" not in text


def test_deliver_as_draft_is_stamped_in_the_loop_body_not_just_documented(
    loop: str,
) -> None:
    """The flag must be stamped onto the loop's outputs, or the S-series draft
    guarantee silently becomes unenforceable downstream (sdk-review, F3 — the
    first fix documented the stamp without performing it, so this test reads
    the DELEGATION BODY, not the parameter prose)."""
    body = loop.split("### Delegation")[1]
    # Surviving results are stamped where classification_override lands...
    assert "let result.deliver_as_draft = true" in body
    # ...residue items are stamped before the report is emitted...
    assert "let item.deliver_as_draft = true" in body
    # ...and the emitted report carries the promised column.
    emit = body.split("emit residue as structured report")[1]
    assert "deliver_as_draft" in emit


def test_deliver_as_draft_names_its_consumer(loop: str) -> None:
    """Stamping is only half the contract — the prose must say who reads it
    (the playbook's delivery stage and the /remediate residue phase)."""
    assert "residue report" in loop.split("`deliver_as_draft`")[1][:900]


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
    """Only P/F/I/S take the flag.

    Threading it into an area that already applies would be meaningless; missing it
    on one of these four makes `--apply-unverifiable` a partial no-op that looks
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


@pytest.mark.parametrize(
    "area", ["prescriptions", "preflight", "dockerfile", "security"]
)
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


@pytest.mark.parametrize("area", ["prescriptions", "preflight", "security"])
def test_blind_gate_areas_force_the_unverifiable_classification(area: str) -> None:
    """P, F and S must not be able to emit a result that reads as gate-verified.

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


# ── the area's tier list must match the catalog ────────────────────────────


def test_dependency_area_lists_every_warn_tier_d_rule() -> None:
    """A WARN-tier rule missing from this list is skipped by strict-mode
    `/remediate`, silently.

    The list drives which findings the area processes in strict mode. Adding a
    WARN rule to the catalog and forgetting the prose costs nothing at import
    time, produces no warning, and the only symptom is that the rule is never
    remediated — indistinguishable from a repo that has no such finding. D014
    shipped exactly that way and was caught in review, not by a test.
    """
    from conformance.suite.rules import CATALOG

    text = _read("areas/dependency.prose.md")
    listed = set(re.findall(r"\bD\d{3}\b", text.split("### The re-detection")[0]))
    expected = {
        rule_id
        for rule_id, rule in CATALOG.items()
        if rule_id.startswith("D") and rule.tier.value.upper() == "WARN"
    }
    missing = sorted(expected - listed)
    assert not missing, (
        f"WARN-tier D-rule(s) {missing} are not named in the dependency area's "
        "violation-set, so strict-mode /remediate will skip their findings. Add "
        "them to the WARN-tier list and give each a Fix Prescription entry."
    )


def test_dependency_area_has_a_prescription_for_every_d_rule() -> None:
    """Being listed as WARN-tier is only half of it: the area also has to say
    what to do with the finding, or the loop reaches it with no instruction."""
    from conformance.suite.rules import CATALOG

    text = _read("areas/dependency.prose.md")
    prescriptions = set(re.findall(r"\*\*(D\d{3}) [A-Za-z]", text))
    expected = {rule_id for rule_id in CATALOG if rule_id.startswith("D")}
    missing = sorted(expected - prescriptions)
    assert not missing, (
        f"D-rule(s) {missing} have no Fix Prescription entry in the dependency "
        "area. Every rule the loop can reach needs one, even if it is "
        "`not_remediable = true` and routes straight to residue."
    )
