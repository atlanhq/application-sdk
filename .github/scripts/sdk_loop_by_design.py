"""Drop findings the team has already decided are not findings.

`.mothership/pr-review/references/retro-log.md` is 185 lines asking the reviewer
to remember a list of exceptions and withdraw its own candidate findings. That
mechanism has three problems, and this module exists because of all three:

1. **It is the weakest available enforcement.** Asking a model to reliably
   *not* say something is a request, not a guarantee, and it competes with
   every other instruction in the prompt.
2. **A miss is invisible.** Nothing errors when the reviewer forgets — the
   suppressed finding simply appears, indistinguishable from a real one, and
   the author pays a round to say "that's intentional" for the ninth time.
3. **It is paid for on every round.** The list sits in context whether or not
   the diff touches anything it covers.

Moving it here makes the suppression a fact rather than a hope, removes it from
the reviewer's context entirely, and — because every drop is recorded with the
entry that caused it — makes over-suppression auditable in a way that a model
silently staying quiet never can be.

Google's static-analysis programme frames the target as the *effective* false
positive rate: any finding after which the developer took no positive action,
whether or not it was technically correct. Their contract is that an analyser
above roughly 10% gets switched off. Everything in `by_design.yaml` is a pattern
measured at zero action across real reviews.

Two invariants keep this from becoming a way to hide real defects:

* **A guardrail finding is never suppressed.** Guardrails are merge-blocking
  facts about the code and are reported regardless of confidence; silently
  dropping one is the worst failure this module could have. If a guardrail is
  firing wrongly, that is a rubric bug to fix in `severity.yaml`, not something
  to paper over here.
* **A `ci-gate` entry may not claim a rule CI does not block.** That claim is
  the only kind in the file that can be factually false, and when it is false
  the rule stops being enforced by anyone — CI does not block it and the
  reviewer has been told to stay quiet. `load_by_design` raises instead.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

#: Shipped alongside the playbook so the lane's data travels with the lane.
DEFAULT_PATH = (
    Path(__file__).resolve().parents[2] / ".mothership/pr-loop/data/by_design.yaml"
)

#: The `owner` values an entry may declare. Each is a different KIND of claim,
#: and only `ci-gate` asserts something about CI that can be wrong.
OWNERS = frozenset({"ci-gate", "by-design", "tech-debt"})


class ByDesignError(ValueError):
    """The data file is malformed, or makes a claim that is not true."""


class _HasFindingFields(Protocol):
    """The slice of `sdk_loop_findings.Finding` this module reads.

    Declared structurally so the filter can be tested without constructing a
    full Finding, and so it does not import the renderer just for a type.
    """

    pattern_id: str | None
    category: str | None
    file: str | None
    evidence: str | None
    attack_path: str | None
    guardrail: str | None


@dataclass(frozen=True)
class Entry:
    """One suppression rule.

    Every criterion present in `match` must hold for the entry to fire (AND,
    never OR). An entry with a single criterion is therefore broad on purpose;
    one with a path *and* a rule id is narrow on purpose. `unless_evidence`
    then inverts the burden for the conditional cases: the pattern is
    suppressed by default and survives only when the reviewer's own evidence
    substantiates the exception.
    """

    id: str
    owner: str
    reason: str
    paths: tuple[str, ...] = ()
    pattern_ids: frozenset[str] = frozenset()
    categories: frozenset[str] = frozenset()
    evidence: re.Pattern[str] | None = None
    unless_evidence: re.Pattern[str] | None = None

    def matches(self, finding: _HasFindingFields) -> bool:
        if self.pattern_ids and (finding.pattern_id or "") not in self.pattern_ids:
            return False
        if self.categories and (finding.category or "") not in self.categories:
            return False
        if self.paths and not _path_matches(finding.file or "", self.paths):
            return False
        if self.evidence is not None and not self.evidence.search(_text(finding)):
            return False
        # Checked last: it can only ever rescue a finding the criteria above
        # already caught, so evaluating it earlier would be wasted work.
        if self.unless_evidence is not None and self.unless_evidence.search(
            _text(finding)
        ):
            return False
        return True


@dataclass
class ByDesign:
    entries: list[Entry] = field(default_factory=list)
    never_ci_owned: frozenset[str] = frozenset()

    def match(self, finding: _HasFindingFields) -> Entry | None:
        """Return the entry that suppresses `finding`, or None to keep it.

        A guardrail short-circuits to None before any entry is consulted — see
        the module docstring. The first matching entry wins; entries are
        checked in file order so the data file reads as its own precedence.
        """
        if getattr(finding, "guardrail", None):
            return None
        for entry in self.entries:
            if entry.matches(finding):
                return entry
        return None


def _text(finding: _HasFindingFields) -> str:
    """Evidence and attack path, searched as one blob.

    A conditional entry asks whether the reviewer substantiated an exception,
    and reviewers put that substantiation in either field depending on whether
    they are describing the code or how it is reached. Searching only
    `evidence` would make `unless_evidence` fire or not fire on where the
    sentence happened to land.
    """
    return "\n".join(p for p in (finding.evidence, finding.attack_path) if p)


def _path_matches(path: str, globs: tuple[str, ...]) -> bool:
    """`**/x/**` semantics over a repo-relative path.

    `fnmatch` treats `*` as matching separators too, which is why a plain
    `fnmatch(path, "**/execution/_temporal/**")` would also match
    `application_sdk/execution/_temporal_shim/thing.py`. Normalising to a
    leading `/` and requiring the segment boundaries to be literal keeps the
    seam suppressions from leaking into adjacent packages.
    """
    subject = "/" + path.lstrip("/")
    for glob in globs:
        pattern = glob if glob.startswith(("/", "*")) else "/" + glob
        if fnmatch.fnmatch(subject, pattern):
            return True
    return False


def _compile(raw: Any, entry_id: str, field_name: str) -> re.Pattern[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ByDesignError(f"{entry_id}: {field_name} must be a string regex")
    try:
        return re.compile(raw)
    except re.error as exc:
        raise ByDesignError(
            f"{entry_id}: {field_name} is not a valid regex: {exc}"
        ) from exc


def load_by_design(path: Path | str | None = None) -> ByDesign:
    """Load and validate the suppression data.

    Validation is deliberately loud. Every failure mode here is one that would
    otherwise be silent at review time: a typo'd owner suppresses nothing, a
    bad glob suppresses everything under it, and a `ci-gate` entry naming a
    WARN-tier rule removes the only enforcement that rule had.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    never = frozenset(data.get("never_ci_owned") or ())

    entries: list[Entry] = []
    seen: set[str] = set()
    for raw in data.get("suppress") or ():
        entry_id = raw.get("id")
        if not entry_id:
            raise ByDesignError("every suppress entry needs an id")
        if entry_id in seen:
            raise ByDesignError(f"duplicate suppress entry id: {entry_id}")
        seen.add(entry_id)

        owner = raw.get("owner")
        if owner not in OWNERS:
            raise ByDesignError(
                f"{entry_id}: owner must be one of {sorted(OWNERS)}, got {owner!r}"
            )
        reason = (raw.get("reason") or "").strip()
        if not reason:
            raise ByDesignError(
                f"{entry_id}: reason is required — a suppression "
                "nobody can justify later is one nobody can review"
            )

        match = raw.get("match") or {}
        pattern_ids = frozenset(match.get("pattern_ids") or ())
        if owner == "ci-gate":
            # The one claim in this file that can be factually wrong.
            overreach = sorted(pattern_ids & never)
            if overreach:
                raise ByDesignError(
                    f"{entry_id}: claims CI owns {overreach}, but never_ci_owned "
                    "lists them as surfaced-not-blocked. A ci-gate entry here "
                    "would leave the rule enforced by nobody. Use owner: "
                    "by-design with a path scope if it is intentional in one place."
                )
        if not any(
            (
                pattern_ids,
                match.get("paths"),
                match.get("categories"),
                match.get("evidence"),
            )
        ):
            raise ByDesignError(
                f"{entry_id}: match is empty, which would suppress every finding"
            )

        entries.append(
            Entry(
                id=entry_id,
                owner=owner,
                reason=reason,
                paths=tuple(match.get("paths") or ()),
                pattern_ids=pattern_ids,
                categories=frozenset(match.get("categories") or ()),
                evidence=_compile(match.get("evidence"), entry_id, "match.evidence"),
                unless_evidence=_compile(
                    raw.get("unless_evidence"), entry_id, "unless_evidence"
                ),
            )
        )
    return ByDesign(entries=entries, never_ci_owned=never)
