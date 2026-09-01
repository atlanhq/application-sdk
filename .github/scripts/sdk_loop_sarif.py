#!/usr/bin/env python3
"""Conformance coverage for one PR head, for the `@sdk-loop` context pack.

The reviewer and the conformance suite have been reviewing the same code
without either knowing what the other found. `packages/conformance` ships 167
detector-backed rules and `fetch_conformance_sarif.py` already knows how to
find and download their SARIF — but nothing wires them to the review, so the
reviewer re-litigates rules a gate has already decided, and the do-not-flag
list that stops it doing so is maintained by hand in `retro-log.md`.

This module answers one question for a given head sha: **what did the
deterministic gate already decide about this exact commit?**

Two things it deliberately does NOT do.

It does not suppress on warn-tier. `retro-log.md` says it plainly: conformance
surfaces WARN findings but does not block, "so the reviewer is the only
enforcement". Measured against the catalog, only **19 of 167 rules are
block-tier and SDK-binding** (19 block/both; there are zero block/sdk) — so the
suppression set for an SDK PR is small, and treating warn-tier as covered would
silently delete the reviewer's largest real surface. Warn-tier findings are
passed through as context, not as suppression.

It does not treat absence as clean. A series only uploads when its paths
changed, and on round N+1 — right after a resolve push — the gate run for the
new head very often does not exist yet. `unavailable` and `complete` are
different states and the pack says which one it is, because a reviewer told
"nothing fired" when nothing *ran* is a reviewer that skips the surface
entirely.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

CATALOG = (
    Path(__file__).resolve().parents[2]
    / "packages/conformance/conformance/docs/rules/by-id"
)

#: `**Tier:** `block` · **Scope:** `both` · ...` on one line of each rule doc.
_TIER_RE = re.compile(r"\*\*Tier:\*\*\s*`(\w+)`\s*·\s*\*\*Scope:\*\*\s*`(\w+)`")

#: Scopes whose rules can fire on `application_sdk/**`. `app` cannot: those
#: rules run against consumer apps, so for an SDK PR they are not coverage at
#: all — counting them would suppress prose nothing actually blocks.
SDK_SCOPES = frozenset({"sdk", "both"})

#: The four states the pack can report. Ordered worst-known to best-known.
STATE_UNAVAILABLE = "unavailable"
STATE_PENDING = "pending"
STATE_PARTIAL = "partial"
STATE_COMPLETE = "complete"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    tier: str
    scope: str

    @property
    def suppresses_prose(self) -> bool:
        """Whether a reviewer may skip this rule because CI already blocks it."""
        return self.tier == "block" and self.scope in SDK_SCOPES


def load_catalog(path: Path | str | None = None) -> dict[str, Rule]:
    """Rule id -> tier/scope, parsed from the generated per-rule docs.

    Reads the docs rather than importing `conformance.suite.schema.catalog`
    because the loop runner has no reason to install the conformance package,
    and a review must not fail because a sibling package did not resolve.
    """
    directory = Path(path or CATALOG)
    rules: dict[str, Rule] = {}
    for doc in sorted(directory.glob("*.md")):
        match = _TIER_RE.search(doc.read_text(encoding="utf-8"))
        if match is None:
            # A doc without the line is a generator change, not a review
            # failure. Skipping it means the rule is treated as uncovered,
            # which errs toward the reviewer doing the work.
            continue
        rules[doc.stem] = Rule(doc.stem, match.group(1), match.group(2))
    return rules


@dataclass
class Coverage:
    """What the gate decided about one head sha."""

    state: str
    run_id: int | None = None
    series_present: list[str] = field(default_factory=list)
    series_missing: list[str] = field(default_factory=list)
    #: Rule ids that fired at block tier and bind the SDK. The reviewer must
    #: not restate these — CI is already refusing the merge over them.
    blocked: list[str] = field(default_factory=list)
    #: Rule ids that fired at warn tier. CI reports and does NOT block, so
    #: these stay the reviewer's job; they are context, never suppression.
    warned: list[str] = field(default_factory=list)
    #: Findings the gate reported on files this PR did NOT touch. Context
    #: only — never suppression. See `classify_findings`.
    elsewhere: int = 0
    detail: str = ""

    @property
    def suppression_set(self) -> frozenset[str]:
        return frozenset(self.blocked)


def run_for_head(
    repo: str,
    head_sha: str,
    *,
    workflow: str = "SDK Gate",
    limit: int = 10,
    gh: Callable[[list[str]], tuple[int, str]],
) -> tuple[int | None, str]:
    """The gate run for this EXACT sha -> `(run_id, state)`.

    `fetch_conformance_sarif.py` discovers "the newest run on a branch that
    carries live SARIF", which is right for publishing a dashboard and wrong
    here: a review is about one commit, and the newest run on the branch can
    easily be a different one.

    Filtering happens **server-side** via `gh run list --commit`, not by pulling
    a window of recent runs and matching `headSha` locally. That was the first
    implementation and it was wrong in a way only a real-run check found: with a
    client-side window, any head older than the last N gate runs reports
    `unavailable` — indistinguishable from "the gate never ran". Probed against
    three real PR heads, a 40-run window missed all three merged ones and
    `--commit` found every one. The distinction matters most exactly when the
    loop is several rounds deep and the head is no longer newest.

    A run that exists but has not finished is `pending`, not `unavailable` — the
    caller may choose to wait for one and never for the other.
    """
    code, out = gh(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            workflow,
            "--commit",
            head_sha,
            "--limit",
            str(limit),
            "--json",
            "databaseId,status,conclusion",
        ]
    )
    if code != 0 or not out.strip():
        return None, STATE_UNAVAILABLE
    try:
        runs = json.loads(out)
    except json.JSONDecodeError:
        return None, STATE_UNAVAILABLE
    if not runs:
        return None, STATE_UNAVAILABLE

    finished = [r for r in runs if r.get("status") == "completed"]
    if not finished:
        return runs[0].get("databaseId"), STATE_PENDING
    return finished[0].get("databaseId"), STATE_COMPLETE


def result_paths(result: dict[str, Any]) -> list[str]:
    """Every file a SARIF result points at."""
    out = []
    for location in result.get("locations", []) or []:
        uri = (
            (location.get("physicalLocation") or {}).get("artifactLocation") or {}
        ).get("uri")
        if uri:
            out.append(uri)
    return out


def classify_findings(
    sarif_docs: dict[str, Any],
    catalog: dict[str, Rule],
    changed_files: Sequence[str],
) -> tuple[list[str], list[str], int]:
    """`(blocked, warned, elsewhere)` — scoped to the files THIS PR changed.

    **`changed_files` is required, and this is the whole correctness story of
    the module.** The conformance suite scans the entire repository, so its
    output describes repo state, not the PR. Measured on the gate run for one
    real PR: 584 results across 137 files, and **zero** of them in any of the
    five files that PR touched.

    Handing those to a reviewer unscoped is wrong twice over. It implies the PR
    caused findings it did not. And far worse, the suppression list would then
    say "CI already blocks E002, do not restate" — silencing the reviewer on a
    bare-except the PR genuinely introduced, because E002 happened to fire in
    an unrelated file. That loses a real finding, silently, which is the exact
    failure this whole ingestion is supposed to prevent.

    So a finding counts only when it lands in a file the PR changed. Everything
    else is counted and reported as context, never as suppression.

    A rule id the catalog does not know is treated as **warned**, never
    blocked. An unknown rule cannot be proven to block, and guessing wrong in
    that direction makes the reviewer skip a surface nothing was checking.
    """
    changed = set(changed_files)
    blocked: list[str] = []
    warned: list[str] = []
    elsewhere = 0
    for doc in sarif_docs.values():
        for run in doc.get("runs", []) or []:
            for result in run.get("results", []) or []:
                rule_id = result.get("ruleId")
                if not rule_id:
                    continue
                paths = result_paths(result)
                # A result with no location cannot be attributed to this PR.
                if not paths or not (set(paths) & changed):
                    elsewhere += 1
                    continue
                rule = catalog.get(rule_id)
                bucket = blocked if (rule and rule.suppresses_prose) else warned
                if rule_id not in bucket:
                    bucket.append(rule_id)
    return sorted(blocked), sorted(warned), elsewhere


def render_section(coverage: Coverage) -> str:
    """The pack's `50-ci-findings.md`.

    Every state gets an explicit instruction. The reviewer must never have to
    infer what an empty findings list means — that inference is exactly how
    "the gate did not run" becomes "the gate found nothing".
    """
    lines = ["# Deterministic gate coverage for this head", ""]

    if coverage.state == STATE_COMPLETE:
        lines += [
            "**State: complete.** Every conformance series that applies to the "
            "changed paths ran against this exact commit.",
            "",
            "Everything below is scoped to the files THIS PR changed. The suite "
            "scans the whole repository, so its unscoped output is mostly "
            "pre-existing state that has nothing to do with this diff.",
            "",
            "Do NOT raise a finding that a block-tier rule below already "
            "reports. CI is refusing the merge over it; restating it costs a "
            "round and tells the author nothing new.",
        ]
    elif coverage.state == STATE_PARTIAL:
        lines += [
            "**State: partial.** Some series ran against this commit; the ones "
            "listed as missing did not.",
            "",
            "Treat the missing surfaces as **unchecked** and review them "
            "yourself. Only the series listed as present carry any suppression.",
        ]
    elif coverage.state == STATE_PENDING:
        lines += [
            "**State: pending.** The gate is still running for this commit.",
            "",
            "Assume no detector coverage. Review every surface yourself. Do "
            "not wait for it — the round is not blocked on CI.",
        ]
    else:
        lines += [
            "**State: unavailable.** No completed gate run exists for this "
            "commit. This is the normal case on the round straight after a "
            "resolve push.",
            "",
            "Assume no detector coverage. Review every surface yourself.",
        ]

    if coverage.detail:
        lines += ["", f"_{coverage.detail}_"]

    if coverage.blocked:
        lines += [
            "",
            "## Already blocked by CI — do not restate",
            "",
            *(f"- `{rule_id}`" for rule_id in coverage.blocked),
        ]
    if coverage.warned:
        lines += [
            "",
            "## Reported by CI at warn tier — still yours to judge",
            "",
            "CI surfaces these and does **not** block on them, so they are not "
            "suppressed. Raise them where they matter, with your own evidence.",
            "",
            *(f"- `{rule_id}`" for rule_id in coverage.warned),
        ]
    if coverage.elsewhere:
        lines += [
            "",
            f"_{coverage.elsewhere} further finding(s) fired on files this PR "
            "does not touch. They are pre-existing repo state, not this PR's "
            "doing, and they suppress nothing here._",
        ]
    if coverage.series_missing:
        lines += [
            "",
            "## Series that did NOT run against this commit",
            "",
            *(f"- `{name}` — unchecked" for name in coverage.series_missing),
        ]
    return "\n".join(lines).rstrip() + "\n"
