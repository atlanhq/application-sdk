"""The one Linear comment a triage run leaves on its ticket."""

from __future__ import annotations

from dataclasses import dataclass, field

from .classify import KILLED, OTHER_MANIFEST, Triage

MARKER = "<!-- vuln-triage: deterministic -->"
SLA_DAYS = {"CRITICAL": 7, "HIGH": 30, "MEDIUM": 90, "LOW": 180}


@dataclass
class Outcome:
    """What the run did beyond classifying, for the report."""

    allowlist_pr: str = ""  # URL, or "" when none was opened
    allowlist_entries: dict[str, dict] = field(default_factory=dict)
    allowlist_skipped: dict[str, str] = field(default_factory=dict)
    bump_pr: str = ""
    bump_labelled: bool = False
    bump_problem: str = ""  # why there is no (auto-mergeable) bump PR
    dry_run: bool = False


def state_of(t: Triage, o: Outcome) -> str:
    if t.case == KILLED:
        return "killed"
    if not t.allowlistable:
        return f"tracked (SLA {SLA_DAYS.get(t.severity, '?')}d, not allowlisted)"
    if t.cve in o.allowlist_entries:
        state = f"allowlisted until {o.allowlist_entries[t.cve]['expires']}"
    else:
        state = o.allowlist_skipped.get(t.cve, "not allowlisted")
    if t.case == 1:
        if o.bump_pr:
            state += f"; bump PR {o.bump_pr}"
        elif o.bump_problem:
            state += "; bump needs a human"
    if t.case == 4:
        state += "; awaiting base-image rebuild"
    return state


def needs_human(triages: list[Triage], o: Outcome) -> list[str]:
    out: list[str] = []
    rebuild = sorted({t.package for t in triages if t.case == 4 and t.allowlistable})
    if rebuild:
        out.append(
            "Rebuild and republish `app-runtime-base:3` to clear: "
            + ", ".join(f"`{p}`" for p in rebuild)
            + "."
        )
    replace = sorted({t.package for t in triages if t.case == 3})
    if replace:
        out.append(
            "Pick a maintained replacement for: "
            + ", ".join(f"`{p}`" for p in replace)
            + "."
        )
    other = sorted(
        {
            f"`{t.package}` in `{t.source.removeprefix(OTHER_MANIFEST).strip()}`"
            for t in triages
            if t.source.startswith(OTHER_MANIFEST + " ")
        }
    )
    if other:
        out.append("Update outside the root lock: " + ", ".join(other) + ".")
    if o.bump_problem:
        out.append(f"Case-1 bump: {o.bump_problem}")
    if o.bump_pr and not o.bump_labelled:
        out.append(f"Review and merge the bump PR {o.bump_pr} (not auto-merged).")
    breached = [c for c, why in o.allowlist_skipped.items() if why.startswith("SLA")]
    if breached:
        out.append(
            "SLA already breached, so no allowlist entry was added: "
            + ", ".join(f"`{c}`" for c in breached)
            + "."
        )
    return out


def render(ticket: str, triages: list[Triage], o: Outcome, run_url: str) -> str:
    rows = [
        "| CVE | Severity | Package | Installed | Fixed | Source | Case | Outcome |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in triages:
        case = "killed" if t.case == KILLED else str(t.case)
        rows.append(
            f"| `{t.cve}` | {t.severity or '?'} | `{t.package or '-'}` | "
            f"{t.installed or '-'} | {t.fixed or 'none'} | {t.source or '-'} | "
            f"{case} | {state_of(t, o)} |"
        )
    detail = [
        f"- `{t.cve}`: {t.reason}" + (f" {t.note}" if t.note else "") for t in triages
    ]
    counts = {
        "allowlisted": len(o.allowlist_entries),
        "bump": 1 if o.bump_pr else 0,
        "tracked": sum(1 for t in triages if t.case != KILLED and not t.allowlistable),
        "killed": sum(1 for t in triages if t.case == KILLED),
    }
    human = needs_human(triages, o)
    parts = [
        f"## Vuln triage: {ticket}"
        + (" (dry run, nothing pushed)" if o.dry_run else ""),
        "",
        f"{len(triages)} CVE(s): {counts['allowlisted']} newly allowlisted, "
        f"{counts['bump']} bump PR, {counts['tracked']} tracked only, "
        f"{counts['killed']} killed.",
        "",
    ]
    if o.allowlist_pr:
        parts += [f"**Allowlist PR:** {o.allowlist_pr}", ""]
    if o.bump_pr:
        label = "auto-merges when green" if o.bump_labelled else "needs human review"
        parts += [f"**Bump PR:** {o.bump_pr} ({label})", ""]
    parts += rows + ["", "### Why", ""] + detail
    if human:
        parts += ["", "### Needs a human", ""] + [f"- {h}" for h in human]
    parts += [
        "",
        "Leave this ticket open: reconciliation closes it once a release no longer "
        "ships these CVEs.",
        "",
        f"**Run:** [logs]({run_url}) · no model calls, $0" if run_url else "",
        "",
        MARKER,
    ]
    return "\n".join(parts).rstrip() + "\n"
