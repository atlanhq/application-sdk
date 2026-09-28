"""Findings, their stable identity, and the per-PR state that carries across rounds.

A finding's fingerprint is `sha1(category, path, normalised evidence line)` —
not its line number, which moves with every push, and not its prose, which a
model rewrites every time it is asked. Two rounds that see the same defect
therefore agree on its id, which is what lets round 2 ask "is F-3a91 fixed?"
instead of "review this again" — the question that never converges.

State rides in a hidden marker inside lens's own sticky PR comment. Nothing
else stores it, so there is no database to drift from what the PR shows.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from dataclasses import asdict, dataclass, field
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
STATE_MARKER = "lens-state"
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
    rule_id: str = ""
    origin: str = "llm"  # llm | gate
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
    dismissed_by: str = ""  # GitHub login of whoever ran `/lens dismiss`
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
    """Everything lens remembers about one PR."""

    reviewed_head: str = ""
    reviewed_base: str = ""
    model: str = ""
    config_hash: str = ""
    round: int = 0
    dry_rounds: int = 0
    findings: list[Finding] = field(default_factory=list)
    ledger: dict[str, Any] = field(default_factory=dict)
    approach: dict[str, Any] = field(default_factory=dict)  # once-per-PR approach check
    pending_files: list[str] = field(
        default_factory=list
    )  # left unreviewed by a failure
    history: list[dict[str, Any]] = field(default_factory=list)

    def open_findings(self, severities: tuple[str, ...] = SEVERITIES) -> list[Finding]:
        return [
            f for f in self.findings if f.status == "open" and f.severity in severities
        ]

    def encode(self, body_keep: int = BODY_KEEP) -> str:
        """The state as a hidden comment block, kept small: a GitHub comment holds at most
        65,536 characters. Only what a later round reads is stored. An open finding keeps
        its quoted code (free resolution matches it) and a capped body (the verify call
        reads it); the scenario and suggestion were already posted inline. A resolved
        finding keeps only what the Resolved table shows."""
        data = asdict(self)
        for f in data["findings"]:
            f["scenario"] = f["suggestion"] = ""
            if f["status"] == "open":
                f["body"] = f["body"][:body_keep]
            else:
                f["body"] = ""
                f["evidence"] = f["evidence"][:EVIDENCE_KEEP_RESOLVED]
        raw = json.dumps(data, separators=(",", ":")).encode()
        return f"<!-- {STATE_MARKER}:{base64.b64encode(zlib.compress(raw, 9)).decode()} -->"

    @classmethod
    def decode(cls, comment_body: str) -> "PRState | None":
        m = _STATE_RE.search(comment_body or "")
        if not m:
            return None
        try:
            raw = json.loads(zlib.decompress(base64.b64decode(m.group(1))))
        except (ValueError, zlib.error):
            return None
        raw["findings"] = [Finding(**f) for f in raw.get("findings", [])]
        return cls(**raw)


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
