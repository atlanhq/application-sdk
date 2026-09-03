#!/usr/bin/env python3
"""Severity clamping, verdict computation and summary rendering for `@sdk-loop`.

Everything here used to be the model's job, spread across five sections of
`.mothership/pr-review/ORCHESTRATION.md` — §2f guardrails, §2h's verdict table,
§3a's schema allowlist, §3e's 7.2 KB summary template and §3f's `sed` safety
nets. None of it has an indeterminate answer, and all of it was being paid for
twice: once as context the reviewer carried through every turn, and once as
turns spent producing a comment by hand.

Three things follow from moving it here, and they are the point of the module:

1. **The frozen marker contract becomes structural.** `sdk_review_approve.py`,
   `sdk_review_reconcile.py`, `sdk_review_dedupe_verdicts.py`,
   `sdk_review_verdict_gate.py` and four `sdk-review-*.yml` workflows all find
   their work by parsing HTML comment markers. Rendering those in Python makes
   them unit-testable with no model in the loop, and deletes two whole failure
   classes: the reviewer writing the literal string `<HEAD_SHA>` instead of the
   sha, and an empty `<!-- ANSWERS_TRIGGER: -->` line on a `workflow_dispatch`
   run. §3f carries a `sed` for each; neither is needed once the renderer owns
   the output.

2. **`### Findings` empty <=> READY_TO_MERGE becomes an invariant** rather than
   an instruction the model is asked to honour. It is the resolve loop's
   termination condition, so it is the one rule in the lane that must not be
   probabilistic.

3. **The severity vocabulary gets exactly one mapping.** See `severity.yaml`'s
   `display` block for why there were three.

The module is pure: it computes and renders, and posts nothing. `interpret_review`
owns delivery, and — importantly — owns deciding whether a review actually
happened. An empty findings list renders READY_TO_MERGE, so a caller that treats
"the renderer produced a comment" as proof of work would turn a crashed agent
into a merge-ready verdict. Callers must gate on the completion assertion, not
on this module returning successfully.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml
from sdk_loop_by_design import ByDesign

#: Stamped on a review-only run's verdict. The approval workflow refuses to
#: act on it and telemetry filters on it, so an A/B row can never approve a
#: merged PR or be counted as a real review.
MARK_AB = "<!-- SDK_LOOP_AB -->"

#: Canonical severity data. Never read by a model — see the file's own header.
SEVERITY_DATA = (
    Path(__file__).resolve().parents[2] / ".mothership/pr-loop/data/severity.yaml"
)

#: The only fields the inline-comment handler accepts. §3a of the pr-review
#: playbook is emphatic that unknown fields 422 the request, and every one of
#: the seven brief-level JSON examples in that corpus emits three fields it
#: then has to strip (`scope`, `domain_tag`, `guardrail`). Validating against
#: the allowlist here means a brief cannot teach a payload the poster must undo.
FINDING_FIELDS = frozenset(
    {
        "title",
        "pattern_id",
        "severity",
        "category",
        "confidence",
        "file",
        "line",
        "evidence",
        "attack_path",
        "reachable_from",
        "by_design_check",
        "suggested_fix",
        "escalate_to_linear",
    }
)

#: Fields a finding cannot be rendered without.
REQUIRED_FIELDS = ("title", "severity", "confidence", "file")

VERDICT_READY = "READY_TO_MERGE"
VERDICT_FIXES = "NEEDS_FIXES"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_HUMAN = "NEEDS_HUMAN"
VERDICT_REBASE = "NEEDS_REBASE"

#: Verdicts that are correct with an empty `### Findings`. Both non-READY cases
#: are decided BEFORE any finding exists — `conflicting` comes from
#: mergeStateStatus and `needs_human` from what the reviewer could not
#: determine — so flagging them reports every rebase round as a contract
#: violation and contaminates the measurement this audit exists to produce.
_EMPTY_FINDINGS_IS_VALID = frozenset({"READY_TO_MERGE", "NEEDS_REBASE", "NEEDS_HUMAN"})

#: Verdict precedence when several apply at once. BLOCKED outranks NEEDS_HUMAN
#: because a guardrail violation is a fact about the code, while NEEDS_HUMAN is
#: a fact about what the reviewer could not determine.
_VERDICT_RANK = {
    VERDICT_BLOCKED: 4,
    VERDICT_HUMAN: 3,
    VERDICT_REBASE: 2,
    VERDICT_FIXES: 1,
    VERDICT_READY: 0,
}

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class SchemaError(ValueError):
    """A findings payload the renderer refuses to render.

    Raised rather than repaired on purpose. A payload this module cannot
    validate is a phase failure — the alternative is rendering a partial review
    that reads exactly like a complete one.
    """


# --------------------------------------------------------------------------
# Severity data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Severity:
    """The `display` + `calibration` + `guardrails` view of `severity.yaml`."""

    display: dict[str, dict[str, Any]]
    tier_order: tuple[str, ...]
    guardrails: dict[str, dict[str, Any]]
    patterns: dict[str, dict[str, Any]]
    floors: dict[str, float]

    def tier(self, severity: str) -> str | None:
        """Rendered tier for an emitted severity, or None when prose-only.

        Raises on an unknown severity instead of passing it through. One brief
        in the inherited corpus emits `IMPORTANT`, which belongs to none of the
        three vocabularies that corpus uses; silently rendering it would put a
        finding in the summary that the verdict never counted.
        """
        entry = self.display.get(severity)
        if entry is None:
            raise SchemaError(
                f"unmapped severity {severity!r}; expected one of "
                f"{sorted(self.display)}"
            )
        return entry["tier"]

    def in_findings(self, severity: str) -> bool:
        """Whether this severity renders under `### Findings` — i.e. blocks."""
        entry = self.display.get(severity)
        if entry is None:
            raise SchemaError(f"unmapped severity {severity!r}")
        return bool(entry["in_findings"])

    def lowest_blocking(self) -> str:
        """The least severe severity that still renders under `### Findings`.

        Used to floor a guardrail finding. `display` is written worst-first, so
        the last in-findings entry is the mildest severity that still blocks.
        """
        for name in reversed(list(self.display)):
            if self.display[name]["in_findings"]:
                return name
        raise SchemaError("no severity renders into `### Findings`")

    def inline(self, severity: str) -> bool:
        return bool(self.display[severity]["inline_comment"])

    def floor(self, severity: str) -> float:
        """Confidence floor for a severity.

        INFO carries no floor in the canonical rubric — adding one would make
        pr-review's generated copy diverge from the file it must reproduce — so
        it falls back to LOW's, the nearest tier with the same prose-only
        treatment.
        """
        if severity in self.floors:
            return self.floors[severity]
        if severity == "INFO":
            return self.floors["LOW"]
        raise SchemaError(f"no confidence floor for severity {severity!r}")

    def guardrail_for(self, pattern_id: str | None) -> tuple[str, str] | None:
        """`(guardrail id, forced verdict)` for a pattern, or None."""
        if not pattern_id:
            return None
        for gid, entry in self.guardrails.items():
            if pattern_id in entry["patterns"]:
                return gid, entry["verdict"]
        return None


def load_severity(path: Path | str | None = None) -> Severity:
    raw = yaml.safe_load(Path(path or SEVERITY_DATA).read_text(encoding="utf-8"))
    patterns = {
        pattern["pattern_id"]: pattern
        for category in raw["categories"]
        for pattern in category["patterns"]
    }
    return Severity(
        display=raw["display"],
        tier_order=tuple(raw["tier_order"]),
        guardrails=raw["guardrails"],
        patterns=patterns,
        floors=raw["calibration"]["confidence_floors"],
    )


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass
class Finding:
    title: str
    severity: str
    confidence: float
    file: str
    line: int | None = None
    pattern_id: str | None = None
    category: str | None = None
    evidence: str | None = None
    attack_path: str | None = None
    reachable_from: str | None = None
    by_design_check: str | None = None
    suggested_fix: str | None = None
    escalate_to_linear: bool | None = None
    #: Set by `normalise`, not by the model.
    tier: str | None = None
    guardrail: str | None = None

    def payload(self) -> dict[str, Any]:
        """The finding as the inline-comment handler wants it — allowlist only."""
        out: dict[str, Any] = {}
        for name in FINDING_FIELDS:
            value = getattr(self, name, None)
            if value is not None:
                out[name] = value
        return out


@dataclass
class Dropped:
    finding: Finding
    reason: str


@dataclass
class Normalised:
    kept: list[Finding] = field(default_factory=list)
    prose: list[Finding] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)


def parse_finding(raw: dict[str, Any]) -> Finding:
    """Validate one raw finding against the allowlist."""
    if not isinstance(raw, dict):
        raise SchemaError(f"finding must be an object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - FINDING_FIELDS)
    if unknown:
        raise SchemaError(
            f"finding carries fields the handler rejects (422): {unknown}. "
            f"Allowed: {sorted(FINDING_FIELDS)}"
        )
    missing = [name for name in REQUIRED_FIELDS if raw.get(name) in (None, "")]
    if missing:
        raise SchemaError(f"finding missing required field(s): {missing}")
    try:
        confidence = float(raw["confidence"])
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"confidence is not a number: {raw['confidence']!r}") from exc
    if not 0.0 <= confidence <= 1.0:
        raise SchemaError(f"confidence out of range: {confidence}")
    known = {name: raw[name] for name in raw if name in FINDING_FIELDS}
    known["confidence"] = confidence
    return Finding(**known)


def normalise(
    findings: Iterable[Finding],
    sev: Severity,
    by_design: ByDesign | None = None,
) -> Normalised:
    """Clamp severities, apply per-severity floors, and split by destination.

    Order matters and is the reverse of the obvious one: clamp BEFORE the floor
    check. The floor is a property of the severity a finding actually gets, so
    checking it first would hold a clamped-down finding to the floor of a tier
    it no longer occupies — the flat-0.80 mistake the briefs make, in a
    different disguise.

    A guardrail violation skips the floor entirely. `CLAUDE.md`'s guardrail
    table is explicit that they are reported "regardless of confidence score",
    and a guardrail is the one thing whose absence from the summary changes a
    merge decision.
    """
    out = Normalised()
    for finding in findings:
        pattern = sev.patterns.get(finding.pattern_id or "")
        if pattern is not None:
            ceiling = pattern["max_severity"]
            if _rank(finding.severity, sev) > _rank(ceiling, sev):
                finding.severity = ceiling

        guardrail = sev.guardrail_for(finding.pattern_id)
        if guardrail is not None:
            finding.guardrail = guardrail[0]
            # A guardrail is reported regardless of confidence — and, it turns
            # out, regardless of the severity the model picked. The clamp above
            # only ever LOWERS, so a model that under-rates a guardrail pattern
            # (a determinism violation emitted as LOW, say) would land it in
            # `prose`. `compute_verdict` reads only `kept`, so the guardrail's
            # BLOCKED verdict would never fire and a merge-blocking fact would
            # render as a passing remark. Floor it to the mildest severity that
            # still blocks: the model's rating is evidence about how bad the
            # defect is, never about whether the guardrail counts.
            if not sev.in_findings(finding.severity):
                finding.severity = sev.lowest_blocking()

        # After the guardrail is stamped, because the by-design filter refuses
        # to touch guardrail findings and needs the field set to know. Before
        # the floor, because a suppressed finding should be reported as
        # suppressed — "dropped: by-design" is auditable, "dropped: below the
        # HIGH floor" for the same finding hides which mechanism removed it.
        if by_design is not None:
            entry = by_design.match(finding)
            if entry is not None:
                out.dropped.append(
                    Dropped(
                        finding,
                        f"by-design [{entry.id}, {entry.owner}]: {entry.reason}",
                    )
                )
                continue

        if guardrail is None and finding.confidence < sev.floor(finding.severity):
            out.dropped.append(
                Dropped(
                    finding,
                    f"confidence {finding.confidence:.2f} below the "
                    f"{finding.severity} floor {sev.floor(finding.severity):.2f}",
                )
            )
            continue

        finding.tier = sev.tier(finding.severity)
        if sev.in_findings(finding.severity):
            out.kept.append(finding)
        else:
            out.prose.append(finding)
    return out


def _rank(severity: str, sev: Severity) -> int:
    """Order emitted severities so `max_severity` can be compared."""
    order = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL", "BLOCKING"]
    if severity not in order:
        raise SchemaError(f"unmapped severity {severity!r}")
    return order.index(severity)


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


def compute_verdict(
    kept: Sequence[Finding],
    sev: Severity,
    *,
    needs_human: bool = False,
    conflicting: bool = False,
) -> str:
    """§2h's table, as code.

    `READY_TO_MERGE` is strict and stays strict: **any** finding rendered under
    `### Findings` forces `NEEDS_FIXES`, whatever its tier. That is what makes
    the resolve loop terminate — it fixes until the list is empty — and it is
    why `severity.yaml` routes LOW and INFO to prose instead. A tier that
    renders into Findings blocks the merge; there is no third state.
    """
    if conflicting:
        # Decided from mergeStateStatus before the model is ever called, so it
        # is not a finding and cannot be outvoted by one.
        return VERDICT_REBASE

    verdict = VERDICT_READY
    if needs_human:
        verdict = _worse(verdict, VERDICT_HUMAN)
    if kept:
        verdict = _worse(verdict, VERDICT_FIXES)
    for finding in kept:
        guardrail = sev.guardrail_for(finding.pattern_id)
        if guardrail is not None:
            verdict = _worse(verdict, guardrail[1])
    return verdict


def _worse(current: str, candidate: str) -> str:
    return candidate if _VERDICT_RANK[candidate] > _VERDICT_RANK[current] else current


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

#: The human-readable spelling on the `### Verdict:` line. The machine-readable
#: token in the marker is the underscored form; keeping both in one table is
#: what stops them drifting, which §3e asks the model to do by hand.
_VERDICT_PROSE = {
    VERDICT_READY: "READY TO MERGE",
    VERDICT_FIXES: "NEEDS FIXES",
    VERDICT_BLOCKED: "BLOCKED",
    VERDICT_HUMAN: "NEEDS HUMAN REVIEW",
    VERDICT_REBASE: "NEEDS REBASE",
}


def render_markers(
    verdict: str,
    reviewed_head: str,
    *,
    answers_trigger: str | None = None,
    toolkit_artifact_hash: str | None = None,
    review_only: bool = False,
) -> str:
    """The five-marker block, in the frozen order.

    `answers_trigger` is omitted entirely when absent rather than rendered
    empty. §3e records why: the resolver's push guard uses the marker to tell
    "the round I am waiting on has answered" from "an earlier round's verdict
    landed late", and an empty marker reads as present-but-unparseable, which is
    worse than a missing one. `COMMENT_ID` is blank on every `workflow_dispatch`
    run, so this is the common path, not the edge case.
    """
    if verdict not in _VERDICT_PROSE:
        raise SchemaError(f"unknown verdict {verdict!r}")
    if not _SHA_RE.match(reviewed_head or ""):
        raise SchemaError(
            f"REVIEWED_HEAD must be a 40-char lowercase hex sha, got "
            f"{reviewed_head!r}"
        )
    lines = [
        "<!-- SDK_REVIEW -->",
        f"<!-- VERDICT: {verdict} -->",
        f"<!-- REVIEWED_HEAD: {reviewed_head} -->",
    ]
    # Strip BEFORE testing for presence. A whitespace-only COMMENT_ID is the
    # blank `workflow_dispatch` value arriving with padding, not a malformed
    # trigger id — treating it as the latter fails a run that should simply
    # omit the line.
    trigger = str(answers_trigger or "").strip()
    if trigger:
        if not trigger.isdigit():
            raise SchemaError(f"ANSWERS_TRIGGER must be raw digits, got {trigger!r}")
        lines.append(f"<!-- ANSWERS_TRIGGER: {trigger} -->")
    if toolkit_artifact_hash:
        lines.append(f"<!-- TOOLKIT_ARTIFACT_HASH: {toolkit_artifact_hash} -->")
    if review_only:
        # Last, and additive: every existing parser reads the markers above by
        # regex, not by position, so an extra trailing line changes nothing for
        # them. The approval path reads THIS one and stands down.
        lines.append(MARK_AB)
    return "\n".join(lines)


def render_findings(kept: Sequence[Finding], sev: Severity) -> str:
    """§3e's mandatory file-grouped bullet format.

    Files sort alphabetically; within a file, by tier then line. An empty list
    renders as an empty section — deliberately, because that emptiness is the
    signal the resolve loop terminates on.
    """
    if not kept:
        return ""
    by_file: dict[str, list[Finding]] = {}
    for finding in kept:
        by_file.setdefault(finding.file, []).append(finding)

    order = {tier: index for index, tier in enumerate(sev.tier_order)}
    blocks: list[str] = []
    for path in sorted(by_file):
        rows = sorted(
            by_file[path],
            key=lambda f: (order.get(f.tier or "", 99), f.line or 0),
        )
        header = "**PR metadata**" if path == "PR metadata" else f"**`{path}`**"
        bullets = [header]
        for finding in rows:
            tag = f" [{finding.category}]" if finding.category else ""
            where = f" L{finding.line}" if finding.line else ""
            fix = f" *Path: {finding.suggested_fix}*" if finding.suggested_fix else ""
            bullets.append(f"- **{finding.tier}**{tag}{where} — {finding.title}.{fix}")
        blocks.append("\n".join(bullets))
    return "\n\n".join(blocks)


def render_summary(
    *,
    verdict: str,
    pr_number: int | str,
    pr_title: str,
    reviewed_head: str,
    kept: Sequence[Finding],
    sev: Severity,
    summary: str,
    strengths: Sequence[str] = (),
    prose: Sequence[Finding] = (),
    answers_trigger: str | None = None,
    toolkit_artifact_hash: str | None = None,
    is_rereview: bool = False,
    delta: str | None = None,
    review_note: str | None = None,
    model: str = "",
    run_url: str = "",
    heading_subject: str = "SDK",
    review_only: bool = False,
) -> str:
    """The whole verdict comment.

    Callers pass `heading_subject="Contract Toolkit"` for toolkit scopes; the
    `<!-- SDK_REVIEW -->` marker is unchanged either way because the approval
    workflow parses that stable marker and not the heading.
    """
    kind = "Re-review" if is_rereview else "Review"
    parts = [
        render_markers(
            verdict,
            reviewed_head,
            answers_trigger=answers_trigger,
            toolkit_artifact_hash=toolkit_artifact_hash,
            review_only=review_only,
        ),
        f"## {heading_subject} {kind} (@sdk-loop): PR #{pr_number} — {pr_title}",
        "",
        f"### Verdict: {_VERDICT_PROSE[verdict]}",
        "",
        f"> {summary.strip()}" if summary.strip() else "> (no summary provided)",
        "",
        "---",
        "",
    ]
    if delta:
        parts += ["### Delta from previous review", "", delta.strip(), ""]

    parts += ["### Findings", ""]
    body = render_findings(kept, sev)
    parts += [body, ""] if body else ["_None._", ""]

    if prose:
        parts += ["### Observations (not blocking)", ""]
        parts += [
            f"- {f.file}{f' L{f.line}' if f.line else ''} — {f.title}." for f in prose
        ]
        parts += [""]

    if strengths:
        parts += ["### Strengths", ""] + [f"- {s}" for s in strengths] + [""]

    if review_note:
        parts += ["### Review Note", "", review_note.strip(), ""]

    parts += ["---", f"**Models:** {model or 'unknown'}"]
    if run_url:
        parts.append(f"**Run:** [view workflow logs + cost]({run_url})")
    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------
# Payload entry point
# --------------------------------------------------------------------------


def load_payload(text: str | bytes) -> dict[str, Any]:
    """Parse the agent's `findings.json`, refusing anything ambiguous.

    **Not yet the completion gate, and it must become one before anything
    consumes this.** Nothing reads `findings.json` today — the reviewer still
    posts its own comment — so this only has to parse. The moment the runner
    renders and posts the verdict instead, an empty `findings` list means
    `READY_TO_MERGE`, which means `sdk_review_approve.py` casts the `atlan-ci`
    CODEOWNER approval. An agent that crashes or gives up *after* writing its
    file would then produce a merge-ready verdict from a review that never
    happened.

    `PACK_ID` does not close that hole: it proves the pack loaded, not that the
    work was done. The gate needs a positive assertion the model cannot emit by
    accident — `status: "complete"` plus a `reviewed_files` list that actually
    covers the pack's files — and an empty findings list must be accepted only
    when that assertion is present and covering. `REVIEW.md` already instructs
    the reviewer to emit both; this function does not yet require them.

    Ship that requirement in the same change that moves posting to Python, not
    after it.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"findings payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SchemaError("findings payload must be a JSON object")
    if not isinstance(payload.get("findings", []), list):
        raise SchemaError("`findings` must be a list")
    return payload


# --------------------------------------------------------------------------
# Shadow audit — the contract checker, while the model still writes the comment
# --------------------------------------------------------------------------
#
# Step 1 of the migration runs the renderer BEHIND the existing playbook: the
# reviewer still composes and posts its own comment, and this function checks
# that comment against the contract the renderer would have guaranteed.
#
# It cannot compare byte-for-byte — the comment carries model prose (the
# summary, `### Strengths`, finding bodies) that no renderer reproduces, so a
# byte assertion would fail on the first run and the natural response would be
# to weaken it until it passed. What it compares is the part that has one
# correct answer: the markers, the verdict token, and the empty-Findings
# invariant.
#
# It is deliberately non-fatal. The point of the step is to MEASURE how often
# the model breaks the contract before anything depends on it not breaking —
# failing rounds on a diagnostic would just make the measurement expensive.

_FINDING_BULLET_RE = re.compile(r"^\s*[-*]\s+\*\*(Critical|Important|Nit)\*\*", re.M)
_PLACEHOLDER_RE = re.compile(r"<!--\s*REVIEWED_HEAD:\s*<[^>]*>\s*-->")


def audit_comment(body: str) -> list[str]:
    """Contract violations in a model-authored verdict comment, worst first.

    Every check here corresponds to a real failure this lane has already paid
    for, and each one is something the renderer makes impossible by
    construction. An empty list means the model produced what Python would
    have.
    """
    problems: list[str] = []
    text = body or ""

    if "<!-- SDK_REVIEW -->" not in text:
        problems.append("no <!-- SDK_REVIEW --> marker: no consumer will find this")
        return problems
    if not text.lstrip().startswith("<!-- SDK_REVIEW -->"):
        problems.append("the marker block does not lead the comment")

    verdict_match = re.search(r"<!--\s*VERDICT:\s*([A-Z_]+)\s*-->", text)
    if verdict_match is None:
        problems.append("no <!-- VERDICT: X --> marker: nothing can label or approve")
        verdict = None
    else:
        verdict = verdict_match.group(1)
        if verdict not in _VERDICT_PROSE:
            problems.append(f"verdict token {verdict!r} is not one of the five")

    if _PLACEHOLDER_RE.search(text):
        # §3f carries a `sed` for exactly this; the renderer cannot emit it.
        problems.append(
            "REVIEWED_HEAD is the literal placeholder, not a sha — the next "
            "round loses its delta base"
        )
    else:
        head_match = re.search(r"<!--\s*REVIEWED_HEAD:\s*([^\s>]+)\s*-->", text)
        if head_match is None:
            problems.append(
                "no REVIEWED_HEAD marker: next round falls back to a full review"
            )
        elif not _SHA_RE.match(head_match.group(1)):
            problems.append(
                f"REVIEWED_HEAD {head_match.group(1)!r} is not a 40-char lowercase sha"
            )

    if re.search(r"<!--\s*ANSWERS_TRIGGER:\s*-->", text):
        # An empty marker is worse than a missing one: it reads as
        # present-but-unparseable to the resolver's push guard.
        problems.append("ANSWERS_TRIGGER is present but empty; it should be omitted")

    findings_empty = _findings_are_empty(text)
    if verdict is not None:
        if findings_empty and verdict not in _EMPTY_FINDINGS_IS_VALID:
            problems.append(
                f"### Findings is empty but the verdict is {verdict}; the resolve "
                "loop has nothing left to fix and cannot terminate"
            )
        if not findings_empty and verdict == VERDICT_READY:
            problems.append(
                "### Findings is non-empty but the verdict is READY_TO_MERGE — "
                "this would approve a PR with open findings"
            )

    if re.search(r"^\|\s*Severity\s*\|", text, re.M):
        problems.append(
            "findings rendered as a table; §3e mandates file-grouped bullets"
        )

    return problems


def _findings_are_empty(text: str) -> bool:
    """Whether `### Findings` carries any finding bullet.

    Reads the section between `### Findings` and the next `###`/`---`, and
    looks for the `- **Tier**` bullet shape §3e mandates. A section holding only
    prose ("None", "_None._", "No findings") counts as empty, which is the
    common spelling when a re-review clears everything.
    """
    match = re.search(r"^###\s+Findings\s*$", text, re.M)
    if match is None:
        return True
    rest = text[match.end() :]
    end = re.search(r"^(###\s|---\s*$)", rest, re.M)
    section = rest[: end.start()] if end else rest
    return _FINDING_BULLET_RE.search(section) is None
