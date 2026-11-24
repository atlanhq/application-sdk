"""Findings, their stable identity, and the per-PR state that carries across rounds.

A finding's fingerprint is `sha1(category, path, normalised evidence line)` —
not its line number, which moves with every push, and not its prose, which a
model rewrites every time it is asked. Two rounds that see the same defect
therefore agree on its id, which is what lets round 2 ask "is F-3a91 fixed?"
instead of "review this again" — the question that never converges.
Two different findings of one review on the same quote are told apart by
`unique_ids`, so neither is lost.

State rides in a hidden marker inside connector-reviewer's own sticky PR comment. Nothing
else stores it, so there is no database to drift from what the PR shows.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from collections import Counter
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
# What kind of harm a finding's causal chain ends in (`causal_chain.harm`).
# Severity reads two groups in code (agent.bound_by_outcome): a traced
# OUTCOME_FLOOR harm on a normal path is at least high, and a TEST_GAP finding
# is at most medium unless its principle's High case says otherwise.
# `rejection` is a consumer of the output (the publish step, the runtime,
# Atlan) refusing it: the chain's observable outcome, carried past "the
# payload holds X" to what is done with X.
HARMS = (
    "crash",
    "data_loss",
    "silent_wrong_output",
    "rejection",
    "security",
    "hang",
    "leak",
    "performance",
    "compatibility",
    "noise",
    "test_gap",
    "other",
)
OUTCOME_FLOOR = ("crash", "data_loss", "silent_wrong_output", "rejection")
TEST_GAP = "test_gap"
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
    # Why a skeptic check refuted this finding (skeptic.py), "" when it did not.
    # A contested finding was raised as blocking and now stands at medium.
    contested: str = ""
    # What the finding claims happens, checked against the code when it was
    # raised (agent.trace_chain): {"trigger", "steps": [{"at", "code",
    # "effect"}], "outcome", "harm", "normal_path"}. Read by grading and the
    # skeptic; not stored in the state.
    chain: dict[str, Any] = field(default_factory=dict)
    # Raised by a code bundle's retry of an empty final turn
    # (agent._retry_final): the skeptic checks it whatever its severity, and
    # drops it unless upheld. Not stored in the state.
    retry: bool = False

    def fingerprint(self, salt: str | None = None) -> str:
        """The finding's id. `salt` tells apart two different findings of one
        review that share category, path and quote (`unique_ids`); without it
        this is the id every finding had before."""
        ev = " ".join((self.evidence or "").split())[:160]
        key = f"{self.category}|{self.path}|{ev or self.title.lower()}"
        if salt is not None:
            key += f"|{salt}"
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
            f["chain"] = {}
            del f["retry"]
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


def _title_key(title: str) -> str:
    return " ".join((title or "").lower().split())


_RANK = {s: n for n, s in enumerate(SEVERITIES)}


def unique_ids(found: list[Finding]) -> list[Finding]:
    """Give every finding of one review its own id, and return the findings.

    The fingerprint leaves out the prose on purpose, so two DIFFERENT findings
    on the same quoted line of a file, in the same category, got one id, and
    `merge_new` dropped the second as a restatement of the first (FND-5316).
    Groups are formed by the plain fingerprint, not the current id, so a
    second pass over findings an earlier pass already salted (one bundle's,
    then the round's) re-derives the same ids and still sees a repeat.
    Within a group the finding first by principle (no principle sorts
    first), then title, keeps the plain fingerprint, so an open finding on a
    live pull request keeps matching its stored record. Each other one is
    salted with its principle when no other finding of the group has that
    principle, otherwise with its principle and title, and as a last resort
    with a counter. Severity and the order the model writes findings in
    change from round to round and decide nothing here, so a later round that
    reports the same findings gets the same ids. Only a literal repeat (same
    principle and title) is the same finding reported twice, and is kept
    once."""
    groups: dict[str, list[Finding]] = {}
    for f in found:
        groups.setdefault(f.fingerprint(), []).append(f)
    used = set(groups)
    repeats: set[int] = set()
    for base, group in groups.items():
        ordered = sorted(
            group,
            key=lambda f: (
                f.rule_id,
                _title_key(f.title),
                _RANK.get(f.severity, len(SEVERITIES)),
            ),
        )
        distinct: list[Finding] = []
        seen: set[tuple[str, str]] = set()
        for f in ordered:
            ident = (f.rule_id, _title_key(f.title))
            if ident in seen:
                repeats.add(id(f))
            else:
                seen.add(ident)
                distinct.append(f)
        distinct[0].id = base
        shared = Counter(f.rule_id for f in distinct)
        for f in distinct[1:]:
            salt = f.rule_id
            if shared[f.rule_id] > 1:
                salt = f"{f.rule_id}|{_title_key(f.title)}"
            cand, n = f.fingerprint(salt), 2
            while cand in used:
                cand, n = f.fingerprint(f"{salt}#{n}"), n + 1
            f.id = cand
            used.add(cand)
    return [f for f in found if id(f) not in repeats]


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
