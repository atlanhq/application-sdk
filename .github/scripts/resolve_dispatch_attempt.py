#!/usr/bin/env python3
"""Pick which e2e-apps dispatch attempt the rest of the job should report on.

``e2e-apps`` may dispatch a connector run twice (see the action's
``max-attempts`` input). Three later steps then need to agree on *one* attempt:
the artifact fetch, the sticky PR comment, and the step that fails the job. Left
to themselves each would have to re-derive "was there a retry, and did it run?",
and the failure mode of disagreeing is nasty and quiet — a green job whose
comment describes the failed first run, or a red job linking the passing one.

So this resolves it once, into outputs the others read verbatim.

The retry is deliberately visible. A retry that silently converts red to green
turns a genuinely flaky connector into a slow, healthy-looking one, so a masked
failure gets a ``::warning::`` and a step-summary line naming both attempts and
both conclusions.

Reads FIRST_*/RETRY_* from the environment; writes run_id, run_url, status,
conclusion and retried to ``$GITHUB_OUTPUT``.
"""

from __future__ import annotations

import os
import sys


def resolve(env: dict[str, str]) -> dict[str, str]:
    """Return the outputs describing the attempt that counts.

    A retry counts only if it actually produced a run id — the re-key step can
    run and the dispatch still fail, and in that case the first attempt's
    (failing) conclusion must stand rather than being replaced by a blank one.
    """
    retry_run_id = (env.get("RETRY_RUN_ID") or "").strip()
    retried = bool(retry_run_id)
    prefix = "RETRY" if retried else "FIRST"

    return {
        "run_id": retry_run_id if retried else (env.get("FIRST_RUN_ID") or "").strip(),
        "run_url": (env.get(f"{prefix}_RUN_URL") or "").strip(),
        "status": (env.get(f"{prefix}_STATUS") or "").strip(),
        "conclusion": (env.get(f"{prefix}_CONCLUSION") or "").strip(),
        "retried": "true" if retried else "false",
    }


def summarise(env: dict[str, str], resolved: dict[str, str]) -> list[str]:
    """Lines describing a retry, for the log and the step summary. Empty if none."""
    if resolved["retried"] != "true":
        return []

    first = (env.get("FIRST_CONCLUSION") or "").strip() or "unknown"
    final = resolved["conclusion"] or "unknown"
    verdict = (
        "masked a transient first-attempt failure"
        if final == "success"
        else "did not recover"
    )
    return [
        f"Dispatched run was retried: attempt 1 concluded `{first}`, "
        f"attempt 2 concluded `{final}` — the retry {verdict}.",
    ]


def main() -> int:
    env = dict(os.environ)
    resolved = resolve(env)

    for line in summarise(env, resolved):
        print(f"::warning::{line}")
        summary_path = env.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")

    output_path = env.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            for key, value in resolved.items():
                handle.write(f"{key}={value}\n")

    print(
        f"Reporting on {'retry' if resolved['retried'] == 'true' else 'first'} attempt: "
        f"run {resolved['run_id'] or '(none)'} concluded {resolved['conclusion'] or '(none)'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
