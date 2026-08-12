#!/usr/bin/env python3
"""Discover the newest Conformance run with live SARIF and download every series.

Replaces the two inline shell blocks in the ``conformance`` job of
``.github/workflows/update-dashboard.yaml`` (discover + download), per
docs/standards/ci.md — both carried loops and conditionals that could not be
regression-tested from YAML.

Why this exists rather than the old loop
----------------------------------------
The download step used to iterate a **hardcoded list of 7 series slugs**::

    for slug in ci error-handling prescriptions optimizations dependency logging tests

The conformance suite has since grown past that list. A hive run on 2026-08-10
published 10 live SARIF artifacts — ``deprecation``, ``container-image``,
``contract-toolkit`` and ``security`` were silently dropped, ``dependency`` no
longer exists at all, and the step cheerfully logged "Downloaded 6 / 7". Every
per-repo conformance summary on the dashboard was therefore built from a subset
of the evidence, with nothing anywhere reporting the shortfall.

The series list is now derived from the run's own artifacts, so a new series
reaches the dashboard the moment the suite emits it. The only naming contract
is the ``conformance-<series>-sarif`` artifact name, which is asserted by the
tests.

Outputs written to ``$GITHUB_OUTPUT``:

  * ``has_run``         — a run with live SARIF was found
  * ``run_id``          — that run's id
  * ``commit_sha``      — its head SHA
  * ``branch``          — its head branch
  * ``has_artifacts``   — at least one series actually downloaded
  * ``series``          — space-separated series names that downloaded
  * ``discovery_error`` — ``true`` when discovery *itself* failed (the
    ``run list``/artifact-listing call errored or returned unparseable JSON),
    as opposed to the routine "no qualifying run" case. Always ``false`` when a
    run was found. Lets the workflow warn loudly on an operational fault
    (auth/transport/API) without failing the best-effort publish red.

Exits 0 in the "nothing to publish" cases (no candidate run, no live SARIF) —
those are routine and gated downstream by ``has_run`` / ``has_artifacts``. A
download that fails *after* the artifact was confirmed live is not fatal either:
each series is fetched independently, a failure is logged as a ``::warning::``,
and the step publishes whatever subset landed (``has_artifacts`` stays true).
Only when *no* series is retrievable does the step skip (``has_artifacts=false``),
still exiting 0. The one hard failure is the ``run view`` metadata lookup on an
already-confirmed run — a blank ``commit_sha``/``branch`` would bake empty
provenance into the dashboard JSON, so that propagates as nonzero, matching the
prior ``set -euo pipefail`` shell.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ARTIFACT_PREFIX = "conformance-"
ARTIFACT_SUFFIX = "-sarif"


def run_gh(args: list[str]) -> tuple[int, str]:
    """Invoke ``gh`` and return ``(returncode, stdout)``. Stderr is inherited."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["gh", *args], capture_output=True, text=True
    )
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode, proc.stdout


def series_name(artifact_name: str) -> str | None:
    """``conformance-<series>-sarif`` -> ``<series>``; None if it doesn't match.

    Guards against a zero-length series (``conformance--sarif``) and against
    the affixes overlapping on a too-short name.
    """
    if not artifact_name.startswith(ARTIFACT_PREFIX):
        return None
    if not artifact_name.endswith(ARTIFACT_SUFFIX):
        return None
    inner = artifact_name[len(ARTIFACT_PREFIX) : -len(ARTIFACT_SUFFIX)]
    return inner or None


def live_sarif_series(artifacts_payload: dict) -> list[str]:
    """Series names of every non-expired conformance SARIF artifact in a run."""
    out: list[str] = []
    for artifact in artifacts_payload.get("artifacts", []) or []:
        if artifact.get("expired"):
            continue
        name = series_name(artifact.get("name") or "")
        if name and name not in out:
            out.append(name)
    return out


def candidate_runs(payload: list[dict]) -> list[int]:
    """Run ids worth probing — completed runs that either passed or failed.

    A failing Conformance run still uploads its SARIF (the suite reports
    findings by design), so ``failure`` is deliberately kept.
    """
    return [
        entry["databaseId"]
        for entry in payload or []
        if entry.get("conclusion") in ("success", "failure")
        and entry.get("databaseId") is not None
    ]


def write_outputs(pairs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "")
    lines = [f"{k}={v}" for k, v in pairs.items()]
    if path:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


def discover(repo: str, workflow: str, branch: str, limit: int, gh=run_gh):
    """Newest completed run carrying live SARIF -> ``(run_id, series, error)``.

    Returns ``(None, [], False)`` when no run in the window has any — the
    routine case. Probing newest first — rather than trusting ``.[0]`` — keeps
    a repo publishable off an older run when the newest one expired or died
    before uploading.

    The third element flags an *operational* fault in discovery itself: the
    ``run list`` call failing or returning unparseable JSON, or every probe of a
    candidate run erroring. Those are auth/transport/API problems, not
    "nothing to publish", so they surface separately (``discovery_error``) for
    the workflow to warn on loudly, without turning the best-effort publish red.
    """
    rc, out = gh(
        [
            "run",
            "list",
            "--repo",
            repo,
            f"--workflow={workflow}",
            "--status=completed",
            f"--branch={branch}",
            f"--limit={limit}",
            "--json",
            "databaseId,conclusion",
        ]
    )
    if rc != 0:
        print(f"::warning::could not list {workflow} runs for {repo}")
        return None, [], True

    try:
        payload = json.loads(out or "[]")
    except json.JSONDecodeError:
        print(f"::warning::unparseable run list for {repo}")
        return None, [], True

    candidates = candidate_runs(payload)
    probed = 0
    probe_errors = 0
    for run_id in candidates:
        rc, out = gh(["api", f"repos/{repo}/actions/runs/{run_id}/artifacts"])
        if rc != 0:
            print(f"Run {run_id}: artifact listing failed — trying older")
            probed += 1
            probe_errors += 1
            continue
        try:
            artifacts = json.loads(out or "{}")
        except json.JSONDecodeError:
            print(f"Run {run_id}: unparseable artifact listing — trying older")
            probed += 1
            probe_errors += 1
            continue
        probed += 1
        series = live_sarif_series(artifacts)
        if series:
            print(
                f"Run {run_id} has {len(series)} live SARIF artifact(s) "
                f"({', '.join(series)}) — using it"
            )
            return run_id, series, False
        print(f"Run {run_id} has no live SARIF artifacts — trying older")

    # Discovery errored only if EVERY candidate probe failed; at least one
    # listing that parsed (even with zero live SARIF) means the API is healthy
    # and this is the routine "nothing to publish" case.
    error = probed > 0 and probe_errors == probed
    return None, [], error


def download(repo: str, run_id: int, series: list[str], dest: str, gh=run_gh):
    """Download one artifact per series. Returns the series that landed.

    Each series is fetched separately so one unretrievable artifact does not
    cost us the rest of the run's evidence.
    """
    os.makedirs(dest, exist_ok=True)
    got: list[str] = []
    for name in series:
        rc, _ = gh(
            [
                "run",
                "download",
                str(run_id),
                "--repo",
                repo,
                "--name",
                f"{ARTIFACT_PREFIX}{name}{ARTIFACT_SUFFIX}",
                "--dir",
                dest,
            ]
        )
        if rc == 0:
            got.append(name)
        else:
            print(f"::warning::series '{name}' failed to download from run {run_id}")
    return got


def head_ref(repo: str, run_id: int, gh=run_gh) -> tuple[str, str]:
    """Head ``(sha, branch)`` of an already-confirmed run.

    Raises ``RuntimeError`` when the lookup of a run we *already* confirmed has
    live SARIF fails or returns unparseable JSON. The prior shell ran under
    ``set -euo pipefail``, so this ``gh run view`` aborted the step on error;
    swallowing it here would publish dashboard rows with blank provenance.
    """
    rc, out = gh(
        ["run", "view", str(run_id), "--repo", repo, "--json", "headSha,headBranch"]
    )
    if rc != 0:
        raise RuntimeError(f"gh run view failed for run {run_id}")
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"unparseable gh run view payload for run {run_id}") from exc
    sha, branch = payload.get("headSha") or "", payload.get("headBranch") or ""
    if not sha or not branch:
        raise RuntimeError(
            f"gh run view returned blank headSha/headBranch for run {run_id}"
        )
    return sha, branch


def main(argv: list[str] | None = None, gh=run_gh) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--workflow", default="conformance.yaml")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dir", default="/tmp/sarif")
    args = ap.parse_args(argv)

    run_id, series, discovery_error = discover(
        args.repo, args.workflow, args.branch, args.limit, gh=gh
    )
    if run_id is None:
        print(
            f"::warning::No {args.workflow} run with live SARIF artifacts in the "
            f"last {args.limit} completed runs — skipping conformance dashboard update"
        )
        write_outputs(
            {
                "has_run": "false",
                "has_artifacts": "false",
                "discovery_error": "true" if discovery_error else "false",
            }
        )
        return 0

    got = download(args.repo, run_id, series, args.dir, gh=gh)
    print(f"Downloaded {len(got)} / {len(series)} series SARIF artifacts")
    if not got:
        print(f"::warning::No SARIF artifacts retrievable for run {run_id} — skipping")
        write_outputs(
            {"has_run": "true", "run_id": str(run_id), "has_artifacts": "false"}
        )
        return 0

    try:
        sha, branch = head_ref(args.repo, run_id, gh=gh)
    except RuntimeError as exc:
        print(f"::error::{exc} — cannot publish provenance for run {run_id}")
        return 1
    print(f"Conformance run: {run_id}  sha={sha}  branch={branch}")
    write_outputs(
        {
            "has_run": "true",
            "run_id": str(run_id),
            "commit_sha": sha,
            "branch": branch,
            "has_artifacts": "true",
            "series": " ".join(got),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
