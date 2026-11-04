#!/usr/bin/env python3
"""Credential-leak gate of build-and-publish-app.yaml (job ``leak-scan``).

Subcommands (each one is a former inline ``run:`` block):

``gitleaks``
    BLOCKING. Run from the app checkout: scan a pruned copy of the working
    tree (``tests/``, ``test/``, ``.github/`` removed, FND-678) with gitleaks
    and fail on any finding. Writes finding locations only, never values.

``notify-gitleaks``
    Slack alert for a gitleaks block. Reads ``WEBHOOK``, ``REPO``, ``RUN_URL``.

``credscan``
    WARN-ONLY creds-into-logs scan (``credential-leak-gate/scan.py``); writes
    the finding counts as step outputs.

``credscan-report``
    Run-summary section for the creds-into-logs layer. Reads ``WARN``,
    ``CRIT``, ``HIGH``, ``MED``.

``notify-credscan``
    Slack alert for creds-into-logs warnings. Reads ``WEBHOOK``, ``CRIT``,
    ``HIGH``, ``REPO``, ``RUN_URL``.

Neither Slack notifier ever fails the job, and both refuse a webhook that is
not a hooks.slack.com URL. See docs/standards/credential-leak-gate.md.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import _bap

GITLEAKS_REPORT = "/tmp/gitleaks-report.json"  # noqa: S108 - runner scratch
GITLEAKS_SCAN_DIR = "/tmp/gitleaks-scan"  # noqa: S108 - runner scratch
VERDICT = "/tmp/leak-gate-verdict.json"  # noqa: S108 - runner scratch
SCAN_PY = Path(__file__).resolve().parents[1] / "credential-leak-gate" / "scan.py"
SLACK_PREFIX = "https://hooks.slack.com/"
SLACK_LOCATIONS = 10
_PRUNED = ("tests", "test", ".github")


def _load(path: str) -> object:
    """The parsed JSON at ``path``; raises when it is missing or invalid."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _length(value: object) -> int:
    """``jq length`` for the shapes a report can take (null is 0)."""
    if value is None:
        return 0
    if isinstance(value, (list, dict, str)):
        return len(value)
    return abs(value) if isinstance(value, (int, float)) else 0  # type: ignore[return-value]


def _gitleaks_findings(report: str) -> list[dict]:
    try:
        data = _load(report)
    except (OSError, ValueError):
        return []
    return [f for f in data if isinstance(f, dict)] if isinstance(data, list) else []


def _gitleaks_count(report: str) -> int | None:
    """Finding count, or None when the report cannot be read."""
    try:
        return _length(_load(report))
    except (OSError, ValueError):
        return None


def _severe(verdict: object) -> list[dict]:
    """CRITICAL/HIGH creds-into-logs findings, as ``.findings[]? | select(...)``."""
    findings = verdict.get("findings") if isinstance(verdict, dict) else None
    if not isinstance(findings, list):
        return []
    return [
        f
        for f in findings
        if isinstance(f, dict) and f.get("severity") in ("CRITICAL", "HIGH")
    ]


def _post_to_slack(webhook: str, text: str) -> None:
    """POST ``text``; a failure is a warning, never a job failure."""
    proc = _bap.run(
        [
            "curl",
            "-sS",
            "--fail",
            "--max-time",
            "10",
            "-X",
            "POST",
            "-H",
            "Content-type: application/json",
            "--data",
            "@-",
            webhook,
        ],
        input=json.dumps({"text": text}),
    )
    if proc.returncode != 0:
        print("::warning::Slack notification failed (non-blocking).")


def _webhook_or_none() -> str | None:
    webhook = _bap.env("WEBHOOK")
    if not webhook:
        print(
            "::notice::SLACK_LEAK_SCAN_WEBHOOK not set — skipping Slack notification."
        )
        return None
    # Host allowlist: refuse if the secret ever points somewhere but Slack.
    if not webhook.startswith(SLACK_PREFIX):
        print(
            "::warning::SLACK_LEAK_SCAN_WEBHOOK is not a hooks.slack.com URL — "
            "refusing to send."
        )
        return None
    return webhook


# ── Layer 1: gitleaks (blocking) ──────────────────────────────────────────────


def gitleaks(scan_dir: str = GITLEAKS_SCAN_DIR, report: str = GITLEAKS_REPORT) -> int:
    # Scan the working tree, not history: a publish checkout is one shallow
    # commit. Drop .git so gitleaks doesn't walk internals.
    shutil.rmtree(".git", ignore_errors=True)
    # Prune a COPY, never the app checkout itself: the creds-into-logs layer
    # scans `--root app` afterwards and must not be narrowed. Scanning `.` from
    # inside the copy keeps reported paths repo-relative.
    shutil.rmtree(scan_dir, ignore_errors=True)
    shutil.copytree(".", scan_dir, symlinks=True, dirs_exist_ok=True)
    for name in _PRUNED:
        target = Path(scan_dir, name)
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target, ignore_errors=True)

    config_args: list[str] = []
    if Path(scan_dir, ".gitleaks.toml").is_file():
        print("Using app-level .gitleaks.toml allowlist")
        config_args = ["--config", ".gitleaks.toml"]
    # --redact keeps the raw secret off disk; only File/StartLine/RuleID are read.
    rc = _bap.run(
        [
            "gitleaks",
            "dir",
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            report,
            *config_args,
            ".",
        ],
        cwd=scan_dir,
    ).returncode
    count = _gitleaks_count(report) or 0
    if rc == 0 and count == 0:
        print("gitleaks: no hardcoded secrets found.")
        return 0

    # Locations only — the report's Secret/Match fields are never printed.
    _bap.summary(
        "### ❌ Publish blocked — hardcoded secret(s) detected",
        "",
        f"gitleaks found {count} hardcoded secret(s) in shipped application source "
        "(`tests/`, `test/` and `.github/` are not scanned). Locations only "
        "(values withheld):",
        "",
        *(
            f"- `{_bap.jq_str(f.get('File'))}:{_bap.jq_str(f.get('StartLine'))}`"
            f" — rule: {_bap.jq_str(f.get('RuleID'))}"
            for f in _gitleaks_findings(report)
        ),
        "",
        "Remove the secret(s), **rotate** any that were committed, and re-run. "
        "Allowlist confirmed false positives in `.gitleaks.toml`.",
    )
    print(
        f"::error title=Publish blocked::gitleaks found {count} hardcoded secret(s). "
        "See workflow summary."
    )
    return 1


def notify_gitleaks(report: str = GITLEAKS_REPORT) -> int:
    webhook = _webhook_or_none()
    if webhook is None:
        return 0
    count = _gitleaks_count(report)
    locations = "\n".join(
        f"• `{_bap.jq_str(f.get('File'))}:{_bap.jq_str(f.get('StartLine'))}`"
        f" — {_bap.jq_str(f.get('RuleID'))}"
        for f in _gitleaks_findings(report)[:SLACK_LOCATIONS]
    )
    text = (
        ":no_entry: *Publish BLOCKED — hardcoded secret(s)*\n"
        f"*App repo:* `{_bap.env('REPO')}`\n"
        f"gitleaks found {'?' if count is None else count} secret(s) in source. "
        "Remove and *rotate* them — locations below (values withheld):\n"
        f"{locations}\n"
        f"<{_bap.env('RUN_URL')}|View workflow run>"
    )
    _post_to_slack(webhook, text)
    return 0


# ── Layer 2: creds-into-logs (warn-only) ──────────────────────────────────────


def _summary_count(summary: dict, key: str) -> str:
    """``.summary.<key> // 0`` as ``jq -r`` prints it."""
    value = summary.get(key)
    return "0" if value is None or value is False else _bap.jq_str(value)


def credscan(verdict: str = VERDICT, root: str = "app") -> int:
    # Never fails the job: scan.py exits non-zero on CRITICAL/HIGH, which is
    # advisory here. The verdict JSON is the source of truth.
    _bap.run([sys.executable, str(SCAN_PY), "--root", root, "--report", verdict])
    try:
        data = _load(verdict)
    except (OSError, ValueError):
        print(
            "::warning title=Creds-into-logs scan incomplete::No valid verdict "
            "produced; reporting 0 (warn-only, publish not blocked)."
        )
        _bap.output(findings=0, blocking=0)
        return 0
    summary = data.get("summary") if isinstance(data, dict) else None
    summary = summary if isinstance(summary, dict) else {}
    _bap.output(
        findings=_summary_count(summary, "total_findings"),
        blocking=_summary_count(summary, "blocking"),
        critical=_summary_count(summary, "critical"),
        high=_summary_count(summary, "high"),
        medium=_summary_count(summary, "medium"),
    )
    return 0


def _as_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def credscan_report(verdict: str = VERDICT) -> int:
    warn = _bap.env("WARN")
    crit, high, med = (_bap.env(k) for k in ("CRIT", "HIGH", "MED"))
    if _as_int(warn) <= 0:
        _bap.summary(
            "### ✅ Credential leak gate",
            "",
            "gitleaks: ✅ no hardcoded secrets (blocking). Creds-into-logs: ✅ no "
            f"warnings ({med} medium, non-blocking).",
        )
        return 0

    _bap.summary(
        "### ⚠️ Credential leak warnings (non-blocking)",
        "",
        "gitleaks: ✅ no hardcoded secrets (blocking layer passed).",
        "",
        f"Creds-into-logs found {warn} likely finding(s) ({crit} critical, {high} "
        "high). **This does not block the publish** — review and fix, or allowlist "
        "confirmed false positives in `.credential-leak-allow`. Locations only "
        "(values never recorded):",
        "",
    )
    # The verdict produced these counts, so it is readable; if it is not, the
    # step fails as `jq` did.
    data = _load(verdict)
    _bap.summary(
        *(
            f"- `{_bap.jq_str(f.get('file'))}:{_bap.jq_str(f.get('line'))}` — "
            f"{_bap.jq_str(f.get('pattern_id'))} (var: "
            f"{_bap.jq_str(f.get('variable_name'))}, {_bap.jq_str(f.get('severity'))})"
            for f in _severe(data)
        ),
        "",
        "See docs/standards/credential-leak-gate.md.",
    )
    print(
        f"::warning title=Credential-leak warnings::{warn} credential(s) may reach a "
        "log/CLI sink (non-blocking). See run summary."
    )
    return 0


def notify_credscan(verdict: str = VERDICT) -> int:
    webhook = _webhook_or_none()
    if webhook is None:
        return 0
    locations = "\n".join(
        f"• `{_bap.jq_str(f.get('file'))}:{_bap.jq_str(f.get('line'))}` — "
        f"{_bap.jq_str(f.get('pattern_id'))} ({_bap.jq_str(f.get('severity'))})"
        for f in _severe(_load(verdict))[:SLACK_LOCATIONS]
    )
    text = (
        ":warning: *Credential-leak warning — publish NOT blocked*\n"
        f"*App repo:* `{_bap.env('REPO')}`\n"
        f"{_bap.env('CRIT')} critical, {_bap.env('HIGH')} high credential(s) "
        "reaching log/CLI sinks — locations below (values withheld):\n"
        f"{locations}\n"
        f"<{_bap.env('RUN_URL')}|View workflow run>"
    )
    _post_to_slack(webhook, text)
    return 0


COMMANDS = {
    "gitleaks": gitleaks,
    "notify-gitleaks": notify_gitleaks,
    "credscan": credscan,
    "credscan-report": credscan_report,
    "notify-credscan": notify_credscan,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=tuple(COMMANDS))
    args = parser.parse_args(argv)
    return COMMANDS[args.command]()


if __name__ == "__main__":
    _bap.line_buffered_stdout()
    sys.exit(main())
