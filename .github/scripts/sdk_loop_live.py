"""The live path: consume the reviewer's JSON, challenge it, render it, post it.

`REVIEW.md` tells the model its entire output is one JSON object and that it
posts nothing. Until this module, nothing read that object — the runtime still
waited for a comment the model had been told not to write, and every round
under the new playbook would have ended `OUTCOME_FAILED: no verdict comment`.
Shadow mode (#3604) rendered behind a model that still posted; this is the flip
to live, and `load_payload`'s docstring names the one thing that must ship with
it: the completion gate.

## Why the gate is the load-bearing part

An empty `findings` list renders `READY_TO_MERGE`, and `READY_TO_MERGE` is what
casts the `atlan-ci` CODEOWNER approval. So a reviewer that crashes *after*
writing an empty file, or gives up early, or never opens the files it was
handed, would produce a merge-ready verdict from a review that did not happen.
`PACK_ID` does not close that hole — it proves the pack loaded, not that the
work was done.

The gate needs a positive assertion the model cannot emit by accident:

* `status: "complete"` — and `reviewed_files` must actually cover the pack's
  source files. Claiming complete while listing half the files is the gate
  failing, not the review passing.
* `status: "partial"` — honest, and posted, but floored at `NEEDS_HUMAN`. A
  partial review that says so is useful; a partial review that approves is the
  failure the gate exists to stop.
* anything else — no post, `OUTCOME_FAILED`. The old path's failure mode,
  reached deliberately instead of by accident.

## Fail open on the challenge, fail closed on the gate

Those two directions are opposite on purpose. The refuter missing or returning
garbage keeps every finding (`sdk_loop_refute` documents why). The completion
assertion missing drops the round. One protects against losing a real defect;
the other protects against manufacturing an approval. They are different
failures with different costs, and a single "be lenient" policy would get one
of them wrong.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import sdk_loop_redgreen as redgreen
import sdk_loop_refute as refute
from sdk_loop_by_design import ByDesign
from sdk_loop_findings import (
    VERDICT_HUMAN,
    Finding,
    SchemaError,
    Severity,
    compute_verdict,
    load_payload,
    normalise,
    parse_finding,
    render_summary,
)
from sdk_loop_pack import Pack

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"

#: Where the reviewer writes its payload, relative to the workspace. Named in
#: the prompt because `REVIEW.md` says "the path named in your prompt" and,
#: until now, nothing named one.
FINDINGS_RELPATH = ".sdk-loop/findings.json"


@dataclass
class LiveResult:
    """What the live path decided, for `main()` to act on."""

    body: str = ""
    verdict: str = ""
    failure: str = ""
    kept: list[Finding] = field(default_factory=list)
    dropped: int = 0
    challenged: str = refute.NOT_RUN

    @property
    def should_post(self) -> bool:
        return not self.failure and bool(self.body)


# --------------------------------------------------------------------------
# The completion gate
# --------------------------------------------------------------------------


def completion_gate(payload: dict[str, Any], pack: Pack) -> str | None:
    """A reason the review did not happen, or None when it did.

    `reviewed_files` is checked against every non-deleted, non-test pack file,
    not just Python. A reviewer is not asked to re-read a deleted file, and
    listing tests is welcome but not required. When that set is empty (a
    tests-only or deletions-only pack), the remaining pack paths must be
    covered rather than any non-empty list — otherwise a workflow/Helm pack
    would mint READY_TO_MERGE from a dummy reviewed_files entry.
    """
    status = str(payload.get("status") or "").strip().lower()
    if status not in (STATUS_COMPLETE, STATUS_PARTIAL):
        return (
            f"no completion assertion: status={status!r}. An empty findings "
            "list would otherwise render READY_TO_MERGE from a review that "
            "may not have happened."
        )
    reviewed = payload.get("reviewed_files")
    if not isinstance(reviewed, list) or not reviewed:
        return "reviewed_files is missing or empty — nothing asserts the diff was read"

    if status == STATUS_COMPLETE:
        expected = {f.path for f in pack.files if not f.is_test and not f.is_deleted}
        if not expected:
            expected = {f.path for f in pack.files}
        missing = sorted(expected - {str(p) for p in reviewed})
        if missing:
            return (
                f"claims status=complete but reviewed_files omits {missing[:5]}"
                + (" …" if len(missing) > 5 else "")
                + ". Complete means every source file in the pack was examined."
            )
    return None


# --------------------------------------------------------------------------
# Refutation, wired
# --------------------------------------------------------------------------


def refute_prompt(brief: str, kept: Sequence[Finding], diff: str) -> str:
    """The challenger's turn: the brief, the findings keyed for reply, the diff.

    Each finding is presented under the key `arbitrate` will match on, so the
    refuter copies it back verbatim. Index-keyed matching was rejected in
    `sdk_loop_refute` for a reason worth repeating here: the refuter reorders
    and merges entries, and an off-by-one deletes a real defect with an
    argument written about a different one.
    """
    parts = [brief, "", "---", "", "## Findings under challenge", ""]
    for finding in kept:
        parts += [
            f"### target: `{refute.finding_key(finding)}`",
            f"- **{finding.severity}** {finding.title}",
            f"- file: `{finding.file}`" + (f":{finding.line}" if finding.line else ""),
        ]
        if finding.evidence:
            parts.append(f"- evidence: {finding.evidence}")
        if finding.attack_path:
            parts.append(f"- reachability: {finding.attack_path}")
        parts.append("")
    parts += ["---", "", "## The diff", "", "```diff", diff.strip(), "```"]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Red-green, alongside the model
# --------------------------------------------------------------------------


class RedGreenJob:
    """Runs `redgreen.verify` on a thread and hands back the report on join.

    Started the moment the pack exists. `verify` needs `repo, base_ref, tests,
    workdir` and nothing the model produces, so every second it runs during the
    propose and refute stages is a second the review does not wait for it.
    Exceptions are caught and reported as a skipped run rather than raised:
    red-green is advisory, and an advisory stage must never take the review
    down with it.
    """

    def __init__(
        self,
        *,
        repo: Path,
        base_ref: str,
        files: Sequence[Any],
        workdir: Path,
        runner: Any = None,
    ) -> None:
        self._report: redgreen.Report | None = None
        self._error: str = ""

        def work() -> None:
            try:
                tests = redgreen.changed_test_functions(repo, files)
                kwargs: dict[str, Any] = {}
                if runner is not None:
                    kwargs["runner"] = runner
                self._report = redgreen.verify(
                    repo=repo, base_ref=base_ref, tests=tests, workdir=workdir, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 — advisory; must not raise
                self._error = f"{type(exc).__name__}: {exc}"

        self._thread = threading.Thread(
            target=work, name="sdk-loop-redgreen", daemon=True
        )
        self._thread.start()

    def join(self, timeout_s: float) -> redgreen.Report:
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            return redgreen.Report(
                skipped_reason=f"still running after {timeout_s:.0f}s"
            )
        if self._error:
            return redgreen.Report(skipped_reason=f"red-green raised {self._error}")
        return self._report or redgreen.Report(skipped_reason="no report produced")


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def deliver(
    *,
    payload_text: str,
    pack: Pack,
    sev: Severity,
    by_design: ByDesign | None,
    challenge: Callable[[str], str] | None,
    challenge_brief: str,
    challenge_mode: str,
    diff: str,
    redgreen_report: redgreen.Report | None,
    pr: int,
    pr_title: str,
    reviewed_head: str,
    answers_trigger: str | None,
    model: str,
    run_url: str,
    needs_human: bool = False,
    conflicting: bool = False,
    review_only: bool = False,
) -> LiveResult:
    """From the reviewer's file to the comment `main()` will post.

    `challenge` is a callable that takes the refuter prompt and returns its raw
    output — the model call, injected so this whole path runs under test with
    no model. None means no second reviewer was reachable, and the summary says
    so rather than implying the findings were tested.
    """
    try:
        payload = load_payload(payload_text)
    except SchemaError as exc:
        return LiveResult(failure=f"payload rejected: {exc}")

    reason = completion_gate(payload, pack)
    if reason:
        return LiveResult(failure=reason)

    try:
        findings = [parse_finding(raw) for raw in payload.get("findings", [])]
    except SchemaError as exc:
        return LiveResult(failure=f"a finding failed the schema: {exc}")

    normalised = normalise(findings, sev, by_design=by_design)

    # Challenge only what would be rendered. Prose-tier findings never block,
    # so cross-examining them buys precision nobody pays for.
    arbitration = refute.Arbitration(kept=list(normalised.kept), mode=refute.NOT_RUN)
    if challenge is not None and normalised.kept:
        raw = ""
        try:
            raw = challenge(refute_prompt(challenge_brief, normalised.kept, diff))
        except Exception:  # noqa: BLE001 — fail open, per sdk_loop_refute
            raw = ""
        arbitration = refute.arbitrate(
            normalised.kept,
            refute.parse_challenges(raw),
            sev,
            mode=challenge_mode if raw else refute.NOT_RUN,
        )

    kept = arbitration.kept
    partial = str(payload.get("status", "")).lower() == STATUS_PARTIAL
    verdict = compute_verdict(
        kept, sev, needs_human=needs_human or partial, conflicting=conflicting
    )
    if partial and verdict not in ("BLOCKED", "NEEDS_REBASE"):
        # A partial review may not approve. Floored, not forced: a guardrail's
        # BLOCKED still outranks it.
        verdict = VERDICT_HUMAN

    notes = str(payload.get("notes") or "").strip()
    if partial:
        notes = (
            "**Partial review** — the reviewer ran short of budget and said so. "
            "Files it got through are listed under reviewed_files; nothing "
            "below approves the rest.\n\n" + notes
        )

    extras = [refute.render(arbitration)]
    if redgreen_report is not None:
        extras.append(redgreen.render(redgreen_report))
    extras.append(render_dropped(normalised.dropped))
    summary = "\n\n".join(p for p in [notes, *extras] if p)

    body = render_summary(
        verdict=verdict,
        pr_number=pr,
        pr_title=pr_title,
        reviewed_head=reviewed_head,
        kept=kept,
        sev=sev,
        summary=summary,
        strengths=[str(s) for s in payload.get("strengths") or ()],
        prose=normalised.prose,
        answers_trigger=answers_trigger,
        model=model,
        run_url=run_url,
        review_only=review_only,
    )
    return LiveResult(
        body=body,
        verdict=verdict,
        kept=kept,
        dropped=len(normalised.dropped) + len(arbitration.dropped),
        challenged=arbitration.mode,
    )


def render_dropped(dropped: Sequence[Any]) -> str:
    """The audit trail the by-design filter promises, where a human can read it.

    `normalise()` records every finding it drops against the entry that caused
    it. Until this, that record was a property of a data structure nobody
    rendered — "over-suppression is discoverable instead of silent" was true of
    `Normalised.dropped` and false of anything the author saw. Machine
    suppression is only safer than asking a model to stay quiet if the human
    can see what was suppressed, so this is not decoration: it is the clause
    that justifies the filter.

    Collapsed by default. It is there to be checked, not read every time; a
    suppressed list that dominates the summary trains people to skip it.
    """
    if not dropped:
        return ""
    lines = [
        "<details>",
        f"<summary><strong>Suppressed before review ({len(dropped)})</strong> — "
        "by-design and CI-owned patterns, listed so a wrong suppression can be "
        "seen</summary>",
        "",
    ]
    for item in dropped:
        finding = item.finding
        where = f"`{finding.file}`" + (f":{finding.line}" if finding.line else "")
        lines.append(f"- {where} — {finding.title}  \n  _{item.reason}_")
    lines += ["", "</details>"]
    return "\n".join(lines)


def post_comment(repo: str, pr: int, body: str, sh: Callable[..., Any]) -> str:
    """Post via `gh api` with the body on stdin.

    Stdin rather than `-f body=…` because a rendered summary can exceed the
    argument-length limit on a busy PR, and a truncated body would lose the
    marker block that every downstream consumer parses.
    """
    payload = json.dumps({"body": body})
    result = sh(
        ["gh", "api", f"repos/{repo}/issues/{pr}/comments", "--input", "-"],
        input=payload,
    )
    try:
        return str(json.loads(result.stdout).get("html_url") or "")
    except (json.JSONDecodeError, AttributeError):
        return ""
