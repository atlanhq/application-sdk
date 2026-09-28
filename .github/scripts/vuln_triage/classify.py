"""The classification tree the rover used to walk in prose, as code.

    Is the package one of OUR dependencies (in the root uv.lock, at the scanned version)?
    ├── YES ─ fix available?  YES → Case 1: bump PR
    │                         NO  → upstream maintained?  YES → Case 2   NO → Case 3
    ├── vendored inside one of our installed wheels (a Cargo.lock in a wheel)
    │                         → Case 2: clears when that wheel ships the fix
    ├── in another manifest of our tree (packages/conformance/uv.lock, ...)
    │                         → Case 2: clears when that manifest is updated
    └── NO (only in the base image) → Case 4: rebuild app-runtime-base:3

"Fix available" and "we bump uv.lock" are orthogonal: a fixed CVE in a package the base
image carries (Dapr, a base-image copy of a Python package) is Case 4, never Case 1.

A CVE on the ticket that the scan no longer reports is KILLED (case 0).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .scan import Finding, Hit, normalize

KILLED = 0
ALLOWLISTABLE = ("CRITICAL", "HIGH")
ROOT_LOCK = (
    "uv.lock"  # Trivy's Target for the SDK's own lock (fs scan of the repo root)
)
OTHER_MANIFEST = "manifest"  # Triage.source prefix for a hit from any other manifest

# Returns True (maintained), False (unmaintained) or None (could not tell).
UpstreamCheck = Callable[[str], bool | None]


def version_key(v: str) -> tuple[int, ...]:
    """Numeric version tuple. Coarse (pre-release tags are dropped) but it is only used
    to choose between Trivy's fixed-version candidates and to confirm a bump moved up."""
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def pick_fixed(installed: str, fixed: str) -> str:
    """The fixed version to bump to. Trivy lists one per release line ("2.32.4, 3.0.1"):
    prefer the lowest fix on the installed major, then the lowest fix above installed."""
    candidates = [c.strip() for c in fixed.split(",") if c.strip()]
    if not candidates:
        return ""
    cur = version_key(installed)
    above = [c for c in candidates if version_key(c) > cur]
    same_major = [c for c in above if version_key(c)[0] == cur[0]]
    pool = same_major or above or candidates
    return min(pool, key=version_key)


@dataclass
class Triage:
    cve: str
    severity: str
    case: int  # 1-4, or KILLED
    package: str = ""
    installed: str = ""
    fixed: str = ""
    source: str = ""  # "our dependency" / "vendored in <wheel>" / "base image"
    reason: str = ""  # one sentence; becomes the allowlist entry's `reason`
    note: str = ""  # extra detail for the ticket
    bump_to: str = ""  # Case 1 only

    @property
    def allowlistable(self) -> bool:
        return self.case != KILLED and self.severity in ALLOWLISTABLE


def classify_hit(hit: Hit, lock: dict[str, dict], upstream: UpstreamCheck) -> Triage:
    locked = lock.get(normalize(hit.package))
    base = Triage(
        cve="",
        severity="",
        case=4,
        package=hit.package,
        installed=hit.installed,
        fixed=hit.fixed,
    )
    # An fs hit is the root lock's only when Trivy read it from the root uv.lock; the fs
    # scan also reads packages/conformance/uv.lock and anything vendored in .venv.
    from_root_lock = hit.source == "image" or hit.target == ROOT_LOCK
    if from_root_lock and locked and locked["version"] == hit.installed:
        base.source = "our dependency"
        if hit.fixed:
            base.case = 1
            base.bump_to = pick_fixed(hit.installed, hit.fixed)
            base.reason = (
                f"Case 1: our dependency, fixed in {base.bump_to}; clears once a "
                "dependency bump is merged and released (see ticket)."
            )
            return base
        alive = upstream(hit.package)
        if alive is False:
            base.case = 3
            base.reason = (
                f"Case 3: no fix and {hit.package} looks unmaintained upstream; "
                "needs a replacement (see ticket)."
            )
            base.note = "Newest upstream release is past the staleness window."
        else:
            base.case = 2
            base.reason = f"Case 2: no upstream fix for {hit.package} yet; upstream is maintained."
            if alive is None:
                base.note = (
                    "Upstream liveness could not be checked; assumed maintained."
                )
        return base
    if hit.vendored_in and hit.source == "fs":
        wheel = hit.vendored_in
        base.case = 2
        base.source = f"vendored in {wheel}"
        fix = f" >= {pick_fixed(hit.installed, hit.fixed)}" if hit.fixed else ""
        base.reason = (
            f"Case 2: {hit.package} is vendored inside {wheel}; "
            f"clears when {wheel} ships {hit.package}{fix}."
        )
        return base
    if hit.source == "fs":
        # Another manifest in our tree (e.g. packages/conformance/uv.lock). Ours, but
        # not something the root-lock bump can touch, and never a base-image rebuild.
        base.case = 2
        base.source = f"{OTHER_MANIFEST} {hit.target}"
        fix = f" to {pick_fixed(hit.installed, hit.fixed)}" if hit.fixed else ""
        base.reason = (
            f"Case 2: {hit.package} is pinned by {hit.target}, not the SDK's root "
            f"lock; clears when that manifest is updated{fix}."
        )
        return base
    base.case = 4
    base.source = "base image"
    base.reason = "Case 4: base-image CVE; awaiting an app-runtime-base:3 rebuild."
    if locked:
        base.note = (
            f"The base image ships {hit.package} {hit.installed}; "
            f"the SDK lock already has {locked['version']}."
        )
    elif hit.fixed:
        base.note = f"Fixed upstream in {hit.fixed}; a Chainguard rebuild picks it up."
    return base


def classify(
    finding: Finding, lock: dict[str, dict], upstream: UpstreamCheck
) -> Triage:
    """The most actionable hit decides the CVE's case (1 before 2 before 3 before 4);
    the others are recorded in the note so nothing is lost."""
    per_hit = [classify_hit(h, lock, upstream) for h in finding.hits]
    per_hit.sort(key=lambda t: t.case)
    best = per_hit[0]
    best.cve, best.severity = finding.cve, finding.severity
    others = sorted(
        {f"{t.package} {t.installed} ({t.source})" for t in per_hit[1:]}
        - {f"{best.package} {best.installed} ({best.source})"}
    )
    if others:
        extra = "Also found in: " + ", ".join(others) + "."
        best.note = f"{best.note} {extra}".strip()
    return best


def triage_ticket(
    ticket_cves: list[str],
    findings: dict[str, Finding],
    lock: dict[str, dict],
    upstream: UpstreamCheck,
    ticket_severity: str = "",
) -> list[Triage]:
    """Classify exactly the CVEs the ticket tracks, in the ticket's order.

    The ticket's marker, not the whole scan, is the work list: tickets are deduped and
    split by severity, so the scan also carries CVEs other tickets own."""
    out: list[Triage] = []
    for cve in ticket_cves:
        f = findings.get(cve)
        if f is None or not f.hits:
            out.append(
                Triage(
                    cve=cve,
                    severity=ticket_severity.upper(),
                    case=KILLED,
                    reason="No longer reported by the scan this triage read.",
                )
            )
            continue
        out.append(classify(f, lock, upstream))
    return out
