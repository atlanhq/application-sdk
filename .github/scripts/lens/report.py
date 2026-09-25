"""The run report: what lens did, in the job log, the job summary and an artifact.

The PR comment tells the author what lens FOUND. This tells whoever runs lens
what it DID: every model request (stage, status, latency, tokens, cost),
every bundle (turns, tools, why it stopped, what the fact-check removed),
triage and preflight decisions, and where the time went.

Nothing here contains a prompt, source code or a credential: request records
carry counts and statuses only, and any error text is already redacted by the
client.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .review import RunResult


def call_line(event: dict[str, Any]) -> str:
    """One job-log line per model request, printed live as it completes."""
    if event.get("status") == 200:
        return (
            f"lens: call {event['n']:>3} {event['stage']:<28} 200 {event['latency_ms']:>6}ms "
            f"in={event['input']} cached={event['cached']} out={event['output']} "
            f"reasoning={event['reasoning']} tools={event['tool_calls']} ${event['cost']:.5f}"
        )
    return (
        f"lens: call {event['n']:>3} {event['stage']:<28} {event['status']} {event['latency_ms']:>6}ms "
        f"attempt={event['attempt']} {event.get('error', '')}"
    )


def build(res: RunResult) -> dict[str, Any]:
    st = res.state
    led = (st.ledger if st else {}) or {}
    calls = res.calls
    ok = [c for c in calls if c.get("status") == 200]
    inp = sum(c.get("input", 0) for c in ok)
    return {
        "action": res.action,
        "reason": res.reason,
        "mode": res.mode,
        "mode_label": res.mode_label,
        "head": res.head,
        "incomplete": res.incomplete,
        "notes": res.notes,
        "timings_ms": res.timings_ms,
        "ledger": led,
        "totals": {
            "requests": len(calls),
            "failed_requests": len(calls) - len(ok),
            "input_tokens": inp,
            "cached_tokens": sum(c.get("cached", 0) for c in ok),
            "output_tokens": sum(c.get("output", 0) for c in ok),
            "reasoning_tokens": sum(c.get("reasoning", 0) for c in ok),
            "cache_hit_rate": round(sum(c.get("cached", 0) for c in ok) / inp, 3)
            if inp
            else 0.0,
            "cost_usd": round(sum(c.get("cost", 0.0) for c in ok), 6),
            "p50_latency_ms": sorted(c["latency_ms"] for c in ok)[len(ok) // 2]
            if ok
            else 0,
            "max_latency_ms": max((c["latency_ms"] for c in ok), default=0),
        },
        "bundles": [
            {
                "label": b.label,
                "turns": b.turns,
                "turn_budget": b.turn_budget,
                "planned": b.planned,
                "tools": b.tool_calls,
                "stop": b.stop,
                "error": b.error[:200],
                "findings": len(b.findings),
                "unplaced": len(b.unplaced),
                "fact_check_removed": len(b.removed_by_reflector),
            }
            for b in res.bundles
        ],
        "triage": {
            "mechanical": {k: len(v) for k, v in res.triage.mechanical.items()},
            "renames": [f"{a} -> {b}" for a, b in res.triage.renames],
            "duplicates": len(res.triage.duplicate_of),
            "hunks_dropped": res.triage.hunks_dropped,
        },
        "skipped_files": [{"path": p, "why": w} for p, w in res.skipped_files],
        "new_findings": [
            {
                "id": f.id,
                "severity": f.severity,
                "category": f.category,
                "path": f.path,
                "line": f.line,
                "title": f.title,
            }
            for f in res.new_findings
        ],
        "resolved": {"free": res.resolved_free, "verified": res.resolved_verified},
        "approach": (st.approach if st else {}) or {},
        "calls": calls,
    }


def markdown(report: dict[str, Any], pr: int) -> str:
    t = report["totals"]
    tm = report["timings_ms"]
    lines = [
        f"## lens · PR #{pr} · {report['action']} · {report.get('mode_label') or report['mode'] or '-'}",
        "",
    ]
    if report["reason"]:
        lines += [f"**Reason:** {report['reason']}", ""]
    for r in report["incomplete"]:
        lines.append(f"- ⚠️ incomplete: {r}")
    for n in report["notes"]:
        lines.append(f"- ℹ️ {n}")
    lines += [
        "",
        "| requests | failed | input tok | cached | output tok | reasoning tok | cache hit | cost | p50 / max latency | total time |",
        "|---|---|---|---|---|---|---|---|---|---|",
        f"| {t['requests']} | {t['failed_requests']} | {t['input_tokens']} | {t['cached_tokens']} | {t['output_tokens']} "
        f"| {t['reasoning_tokens']} | {t['cache_hit_rate']:.0%} | ${t['cost_usd']:.4f} "
        f"| {t['p50_latency_ms'] / 1000:.1f}s / {t['max_latency_ms'] / 1000:.1f}s | {tm.get('total', 0) / 1000:.1f}s |",
        "",
    ]
    if tm:
        lines.append(
            "**Time by phase:** "
            + " · ".join(f"{k} {v / 1000:.1f}s" for k, v in tm.items() if k != "total")
        )
        lines.append("")
    if report["bundles"]:
        lines += [
            "### Bundles",
            "| bundle | turns | plan | tools | stop | findings | fact-check removed |",
            "|---|---|---|---|---|---|---|",
        ]
        for b in report["bundles"]:
            tools = ", ".join(f"{k}×{v}" for k, v in b["tools"].items()) or "-"
            lines.append(
                f"| {b['label']} | {b['turns']}/{b['turn_budget']} | {'yes' if b['planned'] else '-'} | {tools} "
                f"| {b['stop']}{(' — ' + b['error'][:80]) if b['error'] else ''} | {b['findings']} | {b['fact_check_removed']} |"
            )
        lines.append("")
    tri = report["triage"]
    if tri["mechanical"] or tri["duplicates"]:
        lines.append(
            "**Triage (not sent to the model):** "
            + ", ".join(f"{k}: {v}" for k, v in tri["mechanical"].items())
            + (f", duplicates: {tri['duplicates']}" if tri["duplicates"] else "")
        )
        lines.append("")
    if report["calls"]:
        lines += [
            "<details><summary>Every model request</summary>",
            "",
            "| # | stage | status | latency | in | cached | out | reasoning | cost |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for c in report["calls"]:
            if c.get("status") == 200:
                lines.append(
                    f"| {c['n']} | {c['stage']} | 200 | {c['latency_ms']}ms | {c['input']} | {c['cached']} | {c['output']} | {c['reasoning']} | ${c['cost']:.5f} |"
                )
            else:
                lines.append(
                    f"| {c['n']} | {c['stage']} | {c['status']} | {c['latency_ms']}ms | | | | | {c.get('error', '')[:60]} |"
                )
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def write(
    res: RunResult, pr: int, json_path: str | None, summary_path: str | None
) -> dict[str, Any]:
    rep = build(res)
    if json_path:
        Path(json_path).write_text(json.dumps(rep, indent=2, default=str))
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(markdown(rep, pr))
    return rep
