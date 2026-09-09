"""Cross-examine every finding before it reaches the author.

The highest-precision architecture in the current literature is not a better
single reviewer. It is a proposer, an independent refuter, and an arbiter that
promotes only what survives the exchange — findings must withstand
cross-examination before they are reported at all. The published design rules
are specific and all three are implemented here: roles are separated, a verdict
without written justification does not count, and the standard of evidence is
fixed rather than left to whoever is arguing.

The lane this replaces already had this and was about to lose it. The inherited
corpus runs a cross-model adversarial wave — a second family scoring every
finding AGREE / DISAGREE / PARTIAL — and the redesign dropped it. Precision is
the product for a bot that gates merges, so dropping the one stage that exists
to protect precision is the wrong trade.

## Independence is a spectrum, and the lane decides where it sits

A different model *family* is the strong form: different training, different
blind spots, a genuinely independent read. It needs `$PROXY_BASE`, which the
sandbox lane has and `@sdk-loop` does not.

The weak form is a differently-prompted agent of the same family. Its
independence is real but smaller — it shares the proposer's blind spots and
will agree with some findings for the same wrong reason.

Both are better than none, so the stage runs either way and **records which one
ran**. A summary that says "challenged" without saying how hard is worth less
than one that admits it was the weak form, because the reader calibrates on it.

## Fail open, always

Every ambiguous path keeps the finding:

* the refuter did not run, timed out, or returned nothing
* it returned a verdict for a finding that does not exist, or none for one that
  does
* it disagreed without saying why

A refutation stage that deletes findings when it breaks is worse than no
refutation stage, because the failure is invisible: the review simply reports
less and looks cleaner. Suppression has to be something the refuter did
deliberately and argued for.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

AGREE = "AGREE"
DISAGREE = "DISAGREE"
PARTIAL = "PARTIAL"
STANCES = frozenset({AGREE, DISAGREE, PARTIAL})

#: A DISAGREE shorter than this is not an argument. The refuter is asked to say
#: what context the proposer missed; "false positive" is a vote, and a vote
#: cannot be reviewed later by the human who wonders where a finding went.
MIN_REASON_CHARS = 40

#: How the challenge was run. Recorded so the summary can say how hard the
#: finding was actually tested.
CROSS_FAMILY = "cross-family"
SAME_FAMILY = "same-family"
NOT_RUN = "not-run"


@dataclass(frozen=True)
class Challenge:
    target: str
    stance: str
    reason: str = ""
    severity: str | None = None

    @property
    def is_argued(self) -> bool:
        return len(self.reason.strip()) >= MIN_REASON_CHARS


@dataclass
class Arbitration:
    kept: list[Any] = field(default_factory=list)
    dropped: list[tuple[Any, str]] = field(default_factory=list)
    adjusted: list[tuple[Any, str, str]] = field(default_factory=list)
    mode: str = NOT_RUN
    unmatched: list[str] = field(default_factory=list)


def finding_key(finding: Any) -> str:
    """Stable identity for a finding across the proposer/refuter boundary.

    Deliberately not the list index. The refuter returns free-form JSON and
    reorders, drops and merges entries; an index-keyed match silently applies
    one finding's verdict to another, which is the worst available outcome —
    a real defect deleted by an argument written about something else.
    """
    return f"{finding.file}:{finding.line or 0}:{(finding.title or '').strip()[:80]}"


def parse_challenges(payload: str | dict[str, Any]) -> tuple[Challenge, ...]:
    """Read the refuter's response, tolerating everything except ambiguity.

    Unknown stances are dropped rather than guessed at. A stance this module
    does not recognise means the refuter answered a different question, and
    mapping it onto one of ours would invent an opinion nobody expressed.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(_strip_fence(payload))
        except (json.JSONDecodeError, ValueError):
            return ()
    if not isinstance(payload, dict):
        return ()

    out: list[Challenge] = []
    for raw in payload.get("challenges") or ():
        if not isinstance(raw, dict):
            continue
        stance = str(raw.get("stance", "")).strip().upper()
        target = str(raw.get("target", "")).strip()
        if stance not in STANCES or not target:
            continue
        severity = raw.get("severity")
        out.append(
            Challenge(
                target=target,
                stance=stance,
                reason=str(raw.get("reason") or ""),
                severity=str(severity).strip().upper() if severity else None,
            )
        )
    return tuple(out)


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)


def _strip_fence(text: str) -> str:
    return _FENCE.sub("", text).strip()


def arbitrate(
    findings: Sequence[Any],
    challenges: Sequence[Challenge],
    sev,
    *,
    mode: str = CROSS_FAMILY,
) -> Arbitration:
    """Promote what survived cross-examination.

    Four rules, in order of how much they can cost if wrong:

    1. **A guardrail finding is never dropped.** It is a merge-blocking fact
       about the code, reported regardless of confidence. If a guardrail is
       firing wrongly that is a rubric bug, not something a challenger votes
       away.
    2. **DISAGREE drops the finding only when it is argued.** An unargued
       disagreement is a vote, and votes are not reviewable afterwards by the
       person wondering where a finding went.
    3. **PARTIAL can only lower severity, never raise it.** The proposer read
       the code with the pack in front of it; the refuter is arguing from the
       finding. Letting the weaker context escalate would make the challenge a
       second, worse proposer.
    4. **Anything unmatched is kept.** See the module docstring — fail open.
    """
    result = Arbitration(mode=mode)
    by_target = {c.target: c for c in challenges}
    seen: set[str] = set()

    for finding in findings:
        key = finding_key(finding)
        challenge = by_target.get(key)
        if challenge is not None:
            seen.add(key)

        if getattr(finding, "guardrail", None):
            result.kept.append(finding)
            continue

        if challenge is None or challenge.stance == AGREE:
            result.kept.append(finding)
            continue

        if challenge.stance == DISAGREE:
            if challenge.is_argued:
                result.dropped.append((finding, challenge.reason.strip()))
            else:
                result.kept.append(finding)
            continue

        # PARTIAL
        proposed = finding.severity
        if (
            challenge.severity
            and challenge.severity in sev.display
            and _lower(challenge.severity, proposed, sev)
        ):
            finding.severity = challenge.severity
            result.adjusted.append((finding, proposed, challenge.severity))
        result.kept.append(finding)

    result.unmatched = sorted(set(by_target) - seen)
    return result


def _lower(candidate: str, current: str, sev) -> bool:
    order = list(sev.display)
    try:
        return order.index(candidate) > order.index(current)
    except ValueError:
        return False


def render(result: Arbitration) -> str:
    """One line for the summary, honest about how hard the check was."""
    if result.mode == NOT_RUN:
        return (
            "**Challenge:** not run — no second reviewer was reachable on this "
            "lane. Findings are as first proposed."
        )
    strength = (
        "a different model family"
        if result.mode == CROSS_FAMILY
        else "a second, adversarially-prompted reviewer of the same family"
    )
    parts = [
        f"**Challenge:** every finding was cross-examined by {strength}. "
        f"{len(result.kept)} upheld, {len(result.dropped)} withdrawn"
        + (f", {len(result.adjusted)} downgraded" if result.adjusted else "")
        + "."
    ]
    if result.dropped:
        parts.append("")
        parts.append("Withdrawn after challenge:")
        parts.append("")
        parts += [f"- {f.title} — {reason}" for f, reason in result.dropped]
    return "\n".join(parts)
