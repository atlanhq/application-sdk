"""Plan and apply the allowlist entries that start a Critical/High CVE's SLA clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .classify import Triage

ALLOWLIST_PATH = ".security/base-allowlist.json"
DEFAULT_POLICY = {"CRITICAL": 7, "HIGH": 30}  # same defaults as validate_allowlist.py


def policy_of(data: dict) -> dict[str, int]:
    return {**DEFAULT_POLICY, **data.get("_expiry_policy", {})}


def expires_for(severity: str, detected: date, policy: dict[str, int]) -> date:
    """The SLA deadline. Measured from the ticket's creation — first detection — so a
    re-run on a later day never moves the deadline out."""
    return detected + timedelta(days=int(policy[severity]))


@dataclass
class Plan:
    entries: dict[str, dict]
    skipped: dict[str, str]  # cve -> why it gets no entry this run


def plan_entries(
    triages: list[Triage],
    existing: dict,
    *,
    detected: date,
    today: date,
    ticket: str,
    added_by: str,
) -> Plan:
    policy = policy_of(existing)
    entries: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    for t in triages:
        if not t.allowlistable:
            continue
        if t.cve in existing:
            skipped[t.cve] = "already allowlisted"
            continue
        expires = expires_for(t.severity, detected, policy)
        if expires < today:
            # validate_allowlist.py rejects a past date, and a breached SLA must not
            # be papered over by re-dating it from today. A human decides.
            skipped[t.cve] = f"SLA already breached on {expires.isoformat()}"
            continue
        entries[t.cve] = {
            "package": t.package,
            "severity": t.severity,
            "reason": t.reason,
            "expires": expires.isoformat(),
            "added_by": added_by,
            "case": t.case,
            "ticket": ticket,
        }
    return Plan(entries=entries, skipped=skipped)


def apply(data: dict, entries: dict[str, dict], today: date) -> dict:
    """The allowlist with `entries` added. Metadata keys keep their place at the top."""
    out = dict(data)
    out.update(entries)
    if entries and "_updated" in out:
        out["_updated"] = today.isoformat()
    return out
