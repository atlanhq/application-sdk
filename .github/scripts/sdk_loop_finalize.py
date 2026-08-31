#!/usr/bin/env python3
"""Close out an `@sdk-loop` run: one summary comment, one step summary.

Runs with `always()`, so it is also the reporter for a run that died. A loop
that stopped without saying why is worse than one that failed loudly — the
whole point of the lane is that nobody has to watch it.

The summary is additive to the existing vocabulary, never a replacement: the
per-round verdict comments the review phase posts are the artifacts the merge
gate consumes, and this comment only narrates the run around them.

Environment:
    REPO, PR_NUMBER
    ROUNDS_JSON     per-round records emitted by the phase jobs
    STOP_REASON     why the loop ended
    GH_TOKEN
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

MARKER = "<!-- SDK_LOOP_SUMMARY -->"

#: Every way a run can end, and the one-line explanation a reader gets. Keys
#: are the outcomes the phase jobs emit plus the harness-level stops.
STOP_TEXT = {
    "clean": (
        "**Merge-ready.** The review returned `READY_TO_MERGE` with an empty "
        "`### Findings` — no findings of any tier, nits included."
    ),
    "no_progress": (
        "**Stalled.** A resolve round changed nothing and contested nothing, so "
        "another identical round could not do better."
    ),
    "terminal_verdict": (
        "**Handed back.** The review returned a verdict a resolve phase cannot "
        "fix by changing code."
    ),
    "exhausted": (
        "**Round budget spent.** The loop used all its rounds without reaching an "
        "empty `### Findings`."
    ),
    "reaim_exhausted": (
        "**Kept losing the branch.** Every remaining round was spent re-aiming at "
        "a new commit, so the loop never got a clean pass at one sha."
    ),
    "budget_exhausted": (
        "**Allowance spent.** The run stopped at a round boundary rather than "
        "starting a phase it could not afford. Raise `vars.SDK_LOOP_MAX_USD` and "
        "re-invoke to continue from where it stopped."
    ),
    "failed": (
        "**A phase failed.** The run stopped rather than guessing at a result — "
        "see the round table for which phase and why."
    ),
    "dismissed": (
        "**Stood down.** A loop was already running on this PR; the first one "
        "keeps the branch."
    ),
    "unauthorized": "**Declined.** `@sdk-loop` is restricted to repository collaborators.",
}


@dataclass(frozen=True)
class Round:
    number: int
    phase: str
    outcome: str
    verdict: str = ""
    sha: str = ""
    detail: str = ""
    #: Gateway spend across this phase, or None when it could not be read.
    cost: float | None = None


def parse_rounds(raw: str | None) -> list[Round]:
    if not raw or not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    rounds: list[Round] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("phase"):
            continue
        rounds.append(
            Round(
                number=int(item.get("number", 0) or 0),
                phase=str(item["phase"]),
                outcome=str(item.get("outcome", "")),
                verdict=str(item.get("verdict", "")),
                sha=str(item.get("sha", "")),
                detail=str(item.get("detail", "")),
                cost=_as_cost(item.get("cost")),
            )
        )
    return rounds


def _as_cost(raw: object) -> float | None:
    """A skipped job emits an empty string; an unreadable gateway emits nothing.

    Both mean "no figure", and neither may become 0.0 — a phase that silently
    reports free is worse than one that reports unknown.
    """
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def total_cost(rounds: list[Round]) -> tuple[float, int]:
    """Summed spend, and how many phases could not be measured."""
    measured = [r.cost for r in rounds if r.cost is not None]
    unmeasured = sum(1 for r in rounds if r.outcome and r.cost is None)
    return sum(measured), unmeasured


def _cell(text: str) -> str:
    """Table-safe: a detail string can carry a pipe and break the row."""
    return (text or "—").replace("|", "\\|").replace("\n", " ")


def render_rounds(rounds: list[Round]) -> str:
    if not rounds:
        return "_No round completed._\n"
    lines = [
        "| Round | Phase | Outcome | Verdict | Head | Cost | Detail |",
        "|---|---|---|---|---|--:|---|",
    ]
    for r in rounds:
        cost = "—" if r.cost is None else f"${r.cost:,.4f}"
        lines.append(
            f"| {r.number} | {r.phase} | `{r.outcome}` | {_cell(r.verdict)} "
            f"| `{r.sha[:8] or '—'}` | {cost} | {_cell(r.detail)} |"
        )
    return "\n".join(lines) + "\n"


def render_cost(rounds: list[Round]) -> str:
    """The run total, with the caveat that makes the number honest.

    Cost is measured as the movement in the gateway KEY's cumulative spend
    across each phase. The key is the billing unit, not the run — and this lane
    runs PRs in parallel and shares its key with other automation — so anything
    else billing during a phase lands in that phase's figure. It is an upper
    bound, and saying so is the difference between a useful number and a
    misleading one.
    """
    total, unmeasured = total_cost(rounds)
    if not any(r.cost is not None for r in rounds):
        return (
            "\n**Cost:** unavailable — the gateway did not report key spend. "
            "No figure is better than a wrong one.\n"
        )
    line = f"\n**Cost:** ${total:,.4f} across {sum(1 for r in rounds if r.cost is not None)} phases"
    if unmeasured:
        line += f" ({unmeasured} phase(s) unmeasured)"
    return (
        line
        + ". Measured as the movement in the gateway key's spend, so concurrent "
        + "traffic on the same key is included — read it as an upper bound.\n"
    )


def render(rounds: list[Round], stop_reason: str, run_url: str) -> str:
    headline = STOP_TEXT.get(stop_reason, f"**Stopped** — `{stop_reason}`.")
    reviews = sum(1 for r in rounds if r.phase == "review")
    resolves = sum(1 for r in rounds if r.phase == "resolve")
    reaims = sum(1 for r in rounds if r.outcome == "reaim")

    body = [
        MARKER,
        "## `@sdk-loop`",
        "",
        headline,
        "",
        f"{reviews} review · {resolves} resolve"
        + (f" · {reaims} re-aimed onto a new commit" if reaims else ""),
        "",
        render_rounds(rounds),
        render_cost(rounds),
    ]
    if reaims:
        body.append(
            "\nA re-aim means someone pushed while the loop was working. The loop "
            "discarded that round's work and went back to review on the new "
            "commit — nothing was fixed against a review of a different sha.\n"
        )
    body.append(f"\n[Run log]({run_url})\n")
    return "\n".join(body)


def main(argv: list[str] | None = None) -> int:
    rounds = parse_rounds(os.environ.get("ROUNDS_JSON"))
    stop_reason = os.environ.get("STOP_REASON", "failed")
    run_url = os.environ.get("GHA_RUN_URL", "")
    text = render(rounds, stop_reason, run_url)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text)

    repo, pr = os.environ.get("REPO"), os.environ.get("PR_NUMBER")
    if repo and pr:
        subprocess.run(
            ["gh", "pr", "comment", pr, "--repo", repo, "--body", text],
            check=False,
            capture_output=True,
            text=True,
        )
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
