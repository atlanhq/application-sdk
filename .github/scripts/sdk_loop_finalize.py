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

# The one shared formatter. Finalize is otherwise stdlib-only, but duplicating
# the token rendering is how the USD/token mismatch this fixes got in.
from sdk_loop_common import format_tokens, format_usd

MARKER = "<!-- SDK_LOOP_SUMMARY -->"

#: Every way a run can end, and the one-line explanation a reader gets. Keys
#: are the outcomes the phase jobs emit plus the harness-level stops.
STOP_TEXT = {
    "clean": (
        "**Merge-ready.** The review returned `READY_TO_MERGE` with an empty "
        "`### Findings` — no findings of any tier, nits included."
    ),
    "fast_track": (
        "**Merge-ready without a review.** This PR's own diff is byte-identical "
        "to the one already reviewed `READY_TO_MERGE` — every commit since came "
        "from base — so the verdict was re-stamped on the live head and no round "
        "ran. Push a change and the next `@sdk-loop` reviews it properly."
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
        "starting a phase it could not afford. Raise `vars.SDK_LOOP_MAX_TOKENS` and "
        "re-invoke to continue from where it stopped."
    ),
    "reaim": (
        "**Kept re-aiming.** Every round found the branch somewhere other than "
        "where it had just reviewed, so no round ever got a clean pass at one "
        "commit. Re-invoke once the branch settles."
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
    #: Token counts from opencode itself — exact, and per-phase unlike the
    #: shared gateway key. Carries the cache-hit signal.
    usage: str = ""
    #: List-price dollars for this phase, or None when it could not be priced.
    usd: float | None = None


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
        # A skipped job still emits a row, with every field empty. Counting
        # those produced "8 review · 8 resolve" for a run where three reviews
        # ran and nothing else did — the table then printed five blank rows
        # implying work that never happened.
        if not str(item.get("outcome", "")).strip():
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
                usage=str(item.get("usage", "")),
                usd=_as_cost(item.get("usd")),
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
    """Summed tokens, and how many phases could not be measured."""
    measured = [r.cost for r in rounds if r.cost is not None]
    unmeasured = sum(1 for r in rounds if r.outcome and r.cost is None)
    return sum(measured), unmeasured


def total_usd(rounds: list[Round]) -> tuple[float, int]:
    """Summed list-price dollars, and how many phases carried no price.

    Separate from `total_cost` rather than folded into it: a phase can report
    tokens and no price (an unpriced model) or a price and no tokens (never,
    today, but the field is independent), and collapsing the two counters
    would let one silently stand in for the other.
    """
    measured = [r.usd for r in rounds if r.usd is not None]
    unpriced = sum(1 for r in rounds if r.outcome and r.usd is None)
    return sum(measured), unpriced


def _cell(text: str) -> str:
    """Table-safe: a detail string can carry a pipe and break the row."""
    return (text or "—").replace("|", "\\|").replace("\n", " ")


def render_rounds(rounds: list[Round]) -> str:
    if not rounds:
        return "_No round completed._\n"
    lines = [
        "| Round | Phase | Outcome | Verdict | Head | Tokens | Cost | "
        "Breakdown | Detail |",
        "|---|---|---|---|---|--:|--:|---|---|",
    ]
    for r in rounds:
        cost = "—" if r.cost is None else format_tokens(int(r.cost))
        usd = "—" if r.usd is None else format_usd(r.usd)
        lines.append(
            f"| {r.number} | {r.phase} | `{r.outcome}` | {_cell(r.verdict)} "
            f"| `{r.sha[:8] or '—'}` | {cost} | {usd} | {_cell(r.usage)} "
            f"| {_cell(r.detail)} |"
        )
    return "\n".join(lines) + "\n"


def render_cost(rounds: list[Round]) -> str:
    """The run's tokens AND dollars, each with the caveat that keeps it honest.

    Dollars are back, by a route that needs no gateway permission. The old plan
    read spend from /key/info, which 403s with this lane's non-admin key and,
    when it did not, summed every lane sharing that key. This figure is instead
    the phase's own token counts priced at `MODEL_PRICES_USD_PER_MTOK` — list
    price, stated as such, but attributable to one phase, which /key/info never
    was.

    Two caveats travel with the number and both are printed:

    * It is a LIST-price estimate, not the gateway's bill.
    * It is a FLOOR. `opencode stats` does not attribute what a DISPATCHED
      SUB-AGENT spends, and on a real review that is most of it — which is how
      two complete runs reported a few hundred tokens for reviews lasting
      twenty and forty-five minutes.
    """
    total, unmeasured = total_cost(rounds)
    measured = sum(1 for r in rounds if r.cost is not None)
    if not measured:
        return (
            "\n**Tokens:** unavailable — `opencode stats` reported no usage. "
            "No figure is better than a wrong one.\n"
        )
    line = f"\n**Tokens:** {format_tokens(int(total))} across {measured} phases"
    if unmeasured:
        line += f" ({unmeasured} phase(s) unmeasured)"
    line += (
        ". Billable input + output from `opencode stats`. Sub-agent usage is "
        "NOT attributed by that source, so read this as a floor.\n"
    )
    spend, unpriced = total_usd(rounds)
    priced = sum(1 for r in rounds if r.usd is not None)
    if not priced:
        return line + ("\n**Cost:** unavailable — no phase carried a priced model.\n")
    cost_line = f"\n**Cost:** {format_usd(spend)} across {priced} phases"
    if unpriced:
        cost_line += f" ({unpriced} phase(s) unpriced)"
    return (
        line
        + cost_line
        + (
            ". List price from `MODEL_PRICES_USD_PER_MTOK`, not the gateway's bill,"
            " and a floor for the same sub-agent reason as the token figure.\n"
        )
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
        # NOT check=False-and-forget: a live run generated this whole summary,
        # exited 0, and posted nothing — the failure was indistinguishable
        # from success. The summary is the only place a reader learns what the
        # run did, so a failed post is surfaced loudly even though it must not
        # fail the job (the verdicts are already on the PR either way).
        proc = subprocess.run(
            ["gh", "pr", "comment", pr, "--repo", repo, "--body", text],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(
                f"::error::could not post the run summary to #{pr}: "
                f"{(proc.stderr or '').strip()[:300]}"
            )
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
