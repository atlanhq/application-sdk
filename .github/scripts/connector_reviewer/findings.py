"""Findings, their stable identity, and the per-PR state that carries across rounds.

A finding's fingerprint is `sha1(category, path, normalised evidence line)` —
not its line number, which moves with every push, and not its prose, which a
model rewrites every time it is asked. Two rounds that see the same defect
therefore agree on its id, which is what lets round 2 ask "is F-3a91 fixed?"
instead of "review this again" — the question that never converges.

State rides in a hidden marker inside connector-reviewer's own sticky PR comment. Nothing
else stores it, so there is no database to drift from what the PR shows.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from dataclasses import asdict, dataclass, field, fields
from typing import Any

SEVERITIES = ("critical", "high", "medium", "low")
BLOCKING = ("critical", "high")
CATEGORIES = (
    "bug",
    "security",
    "performance",
    "maintainability",
    "test",
    "style",
    "documentation",
    "other",
)
STATE_MARKER = "connector-reviewer-state"
BODY_KEEP = 600  # characters of an open finding's body kept in the state
EVIDENCE_KEEP_RESOLVED = 160  # the part the fingerprint is built from
_STATE_RE = re.compile(r"<!--\s*" + STATE_MARKER + r":([A-Za-z0-9+/=]+)\s*-->")


@dataclass
class Finding:
    path: str
    line: int  # RIGHT-side start, computed from `evidence`; 0 = not placeable inline
    severity: str
    category: str
    title: str
    body: str
    evidence: str  # verbatim code the finding is about ("existing_code")
    end_line: int = 0
    head_line: int = (
        0  # where the quote sits in the PR-head file, when not inline-commentable
    )
    scope: str = "changed"  # changed | unchanged (an incomplete-fix suggestion on unchanged code)
    scenario: str = ""
    suggestion: str = ""
    # The principle this finding rests on (FP01, RP02, …), checked in code
    # against the principles that apply to its file; "" for a concrete bug.
    rule_id: str = ""
    origin: str = "llm"  # llm | gate
    # The reviewer's claim that the harm lands on something that existed before
    # this PR (callers, a released contract, stored data, replay, wire values).
    compatibility: bool = False
    confidence: float = 0.0
    id: str = ""
    status: str = "open"  # open | fixed | wontfix | stale
    round: int = 1  # the round that found it
    fixed_round: int = (
        0  # the round that resolved it (0 = open, or resolved before this was recorded)
    )
    fixed_by: str = (
        ""  # code-gone | verified | dismissed (a person closed it with a reason)
    )
    dismissed_by: str = ""  # GitHub login of whoever ran `/review dismiss`
    dismiss_reason: str = ""

    def fingerprint(self) -> str:
        ev = " ".join((self.evidence or "").split())[:160]
        key = f"{self.category}|{self.path}|{ev or self.title.lower()}"
        return "F-" + hashlib.sha1(key.encode()).hexdigest()[:6]

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            self.severity = "low"
        if self.category not in CATEGORIES:
            self.category = "other"
        if not self.id:
            self.id = self.fingerprint()


@dataclass
class PRState:
    """Everything connector-reviewer remembers about one PR."""

    reviewed_head: str = ""
    reviewed_base: str = ""
    model: str = ""
    config_hash: str = ""
    round: int = 0
    dry_rounds: int = 0
    findings: list[Finding] = field(default_factory=list)
    ledger: dict[str, Any] = field(default_factory=dict)
    approach: dict[str, Any] = field(default_factory=dict)  # once-per-PR approach check
    # Non-blocking reminders from the PR's changed paths (connector_reviewer/advisory.py): shown in
    # the summary, never findings, so verify never has to close them.
    advisories: list[str] = field(default_factory=list)
    pending_files: list[str] = field(
        default_factory=list
    )  # left unreviewed by a failure
    # Changed files the reviewer meant to read and could not: too large, or past
    # the bundle cap (path -> why). Recomputed every round, since each such file
    # rides along with the next review; while any is listed the verdict is
    # "incomplete", never green. A retry cannot help, so they are not pending.
    unread: dict[str, str] = field(default_factory=dict)
    # `/review waive`: who set the verdict aside, why, and for which head sha
    # ({"by", "reason", "head"}). Empty when nothing is waived. A real review
    # clears it.
    waiver: dict[str, Any] = field(default_factory=dict)
    # Why this pull request was not reviewed, and for which head sha
    # ({"class", "why", "reminder", "head"}); see exempt.py. A real review
    # clears it.
    exempt: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    # The regression and history lines of the last review ({"regression",
    # "history", "head"}), so a summary re-rendered by a dismissal keeps them.
    facts: dict[str, Any] = field(default_factory=dict)

    def open_findings(self, severities: tuple[str, ...] = SEVERITIES) -> list[Finding]:
        return [
            f for f in self.findings if f.status == "open" and f.severity in severities
        ]

    def encode(
        self,
        body_keep: int = BODY_KEEP,
        *,
        evidence_keep: int | None = None,
        drop_resolved: int = 0,
        drop_quotes: int = 0,
        drop_open: int = 0,
    ) -> str:
        """The state as a hidden comment block, kept small: a GitHub comment holds at most
        65,536 characters. Only what a later round reads is stored. An open finding keeps
        its quoted code (free resolution matches it), a capped body (the verify call
        reads it) and a capped suggested change (the summary shows it; there is no
        inline comment holding it). A resolved finding keeps only what the Resolved
        table shows.

        The last resorts, for a state too big for a comment even so: `evidence_keep`
        cuts an open finding's quoted code to its leading whole lines within that many
        characters (a line cut in half would no longer match the code it quotes), and
        `drop_resolved` leaves out that many of the oldest resolved findings. Past
        those, `drop_quotes` stores that many open findings (largest quote first)
        with no quoted code at all: the id and the finding stay, and an empty quote
        is never read as "the code is gone" (see `quote_stored`). `drop_open` leaves
        out that many open findings, lowest severity and newest first."""
        data = asdict(self)
        rows = list(enumerate(data["findings"]))
        gone: set[int] = set()
        if drop_resolved > 0:
            resolved = [i for i, f in enumerate(self.findings) if f.status != "open"]
            gone |= set(resolved[:drop_resolved])
        opened = [i for i, f in enumerate(self.findings) if f.status == "open"]
        if drop_open > 0:
            rank = {s: n for n, s in enumerate(SEVERITIES)}
            least = sorted(
                opened,
                key=lambda i: (
                    -rank.get(self.findings[i].severity, len(SEVERITIES)),
                    -self.findings[i].round,
                    -i,
                ),
            )
            gone |= set(least[:drop_open])
        quoteless: set[int] = set()
        if drop_quotes > 0:
            largest = sorted(
                (i for i in opened if i not in gone),
                key=lambda i: -len(self.findings[i].evidence),
            )
            quoteless = set(largest[:drop_quotes])
        data["findings"] = [f for i, f in rows if i not in gone]
        for i, f in rows:
            if i in gone:
                continue
            f["scenario"] = ""
            if f["status"] == "open":
                f["body"] = f["body"][:body_keep]
                f["suggestion"] = f["suggestion"][:body_keep]
                if i in quoteless:
                    f["evidence"] = ""
                elif evidence_keep is not None:
                    f["evidence"] = _leading_lines(f["evidence"], evidence_keep)
            else:
                f["body"] = f["suggestion"] = ""
                f["evidence"] = f["evidence"][:EVIDENCE_KEEP_RESOLVED]
        raw = json.dumps(data, separators=(",", ":")).encode()
        return f"<!-- {STATE_MARKER}:{base64.b64encode(zlib.compress(raw, 9)).decode()} -->"

    @classmethod
    def decode(cls, comment_body: str) -> "PRState | None":
        # The reviewer appends the state block after everything else, so the
        # last match is the real one. Text earlier in the comment comes from
        # the model and the pull request, and must never be read as state.
        found = _STATE_RE.findall(comment_body or "")
        if not found:
            return None
        try:
            raw = json.loads(zlib.decompress(base64.b64decode(found[-1])))
        except (ValueError, zlib.error):
            return None
        # A field a newer connector-reviewer added (or a revert dropped) must not make every
        # PR's stored state unreadable: keep only the fields this version knows,
        # on the state and on each finding.
        raw["findings"] = [
            Finding(**_known(Finding, f)) for f in raw.get("findings", [])
        ]
        return cls(**_known(cls, raw))


def _leading_lines(text: str, limit: int) -> str:
    """The leading whole lines of `text` that fit in `limit` characters, and
    always the first line: matching is line by line, so a quote cut mid-line
    would stop matching code that is still there."""
    out: list[str] = []
    used = 0
    for line in text.splitlines():
        if out and used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def quote_stored(f: Finding) -> bool:
    """Whether `f` still has its quoted code. A finding is never made without
    one, so an empty quote is one the state left out (`PRState.encode`'s
    `drop_quotes`): there is nothing to match, which proves nothing about
    whether the code is still there."""
    return bool((f.evidence or "").strip())


def _known(cls: type, raw: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in raw.items() if k in names}


def merge_new(state: PRState, fresh: list[Finding], *, round_no: int) -> list[Finding]:
    """Fold a round's findings into the frozen set and return the ones that are new.

    An id already known keeps its original record (and its status); only its
    location is refreshed. That is the convergence rule's mechanical half —
    a re-worded restatement of an old finding cannot become a new one.
    """
    known = {f.id: f for f in state.findings}
    added: list[Finding] = []
    for f in fresh:
        if f.id in known:
            old = known[f.id]
            old.line = f.line
            continue
        f.round = round_no
        state.findings.append(f)
        known[f.id] = f
        added.append(f)
    return added
