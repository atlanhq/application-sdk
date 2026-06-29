"""Tests for .github/scripts/reconcile_allowlist.py (debounce + ticket plan)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import reconcile_allowlist as rec

# ---------------------------------------------------------------------------
# cve_ids_from_trivy
# ---------------------------------------------------------------------------


def test_cve_ids_from_trivy():
    data = {
        "Results": [
            {
                "Vulnerabilities": [
                    {"VulnerabilityID": "CVE-1"},
                    {"VulnerabilityID": "CVE-2"},
                ]
            },
            {"Vulnerabilities": [{"VulnerabilityID": "CVE-2"}]},
            {"Vulnerabilities": None},
        ]
    }
    assert rec.cve_ids_from_trivy(data) == {"CVE-1", "CVE-2"}


def test_cve_ids_from_empty():
    assert rec.cve_ids_from_trivy({}) == set()


# ---------------------------------------------------------------------------
# resolved_cves — the 3-scan debounce
# ---------------------------------------------------------------------------


def test_resolved_when_absent_in_all_three_scans():
    allowlisted = {"CVE-GONE", "CVE-STILL"}
    scans = [{"CVE-STILL"}, {"CVE-STILL"}, {"CVE-STILL"}]  # CVE-GONE absent in all
    assert rec.resolved_cves(allowlisted, scans, debounce=3) == {"CVE-GONE"}


def test_not_resolved_if_present_in_one_recent_scan():
    allowlisted = {"CVE-X"}
    scans = [set(), {"CVE-X"}, set()]  # reappeared in the middle scan
    assert rec.resolved_cves(allowlisted, scans, debounce=3) == set()


def test_not_resolved_with_insufficient_scan_history():
    # Only 2 scans available but debounce wants 3 → fail-safe, remove nothing.
    allowlisted = {"CVE-X"}
    scans = [set(), set()]
    assert rec.resolved_cves(allowlisted, scans, debounce=3) == set()


def test_only_allowlisted_cves_are_candidates():
    allowlisted = {"CVE-A"}
    scans = [set(), set(), set()]  # CVE-B never allowlisted, so never "resolved"
    out = rec.resolved_cves(allowlisted, scans, debounce=3)
    assert out == {"CVE-A"}
    assert "CVE-B" not in out


def test_debounce_of_one_resolves_on_single_clean_scan():
    assert rec.resolved_cves({"CVE-X"}, [set()], debounce=1) == {"CVE-X"}


# ---------------------------------------------------------------------------
# plan_ticket_updates
# ---------------------------------------------------------------------------


def _ticket(ident: str, ids: list[str]) -> dict:
    marker = f"<!-- vuln-ids: {','.join(ids)} -->"
    return {
        "id": f"id-{ident}",
        "identifier": ident,
        "description": f"body\n{marker}\n",
    }


def test_ticket_fully_resolved_is_closed():
    tickets = [_ticket("BLDX-1", ["CVE-A", "CVE-B"])]
    to_close, to_update = rec.plan_ticket_updates(tickets, {"CVE-A", "CVE-B"})
    assert [t["identifier"] for t in to_close] == ["BLDX-1"]
    assert to_update == []


def test_ticket_partially_resolved_is_updated():
    tickets = [_ticket("BLDX-2", ["CVE-A", "CVE-B"])]
    to_close, to_update = rec.plan_ticket_updates(tickets, {"CVE-A"})
    assert to_close == []
    assert to_update[0]["identifier"] == "BLDX-2"
    assert to_update[0]["remaining_ids"] == ["CVE-B"]


def test_ticket_with_no_resolved_cves_untouched():
    tickets = [_ticket("BLDX-3", ["CVE-A"])]
    to_close, to_update = rec.plan_ticket_updates(tickets, {"CVE-Z"})
    assert to_close == [] and to_update == []


def test_ticket_with_no_marker_untouched():
    tickets = [{"id": "x", "identifier": "BLDX-4", "description": "no marker here"}]
    to_close, to_update = rec.plan_ticket_updates(tickets, {"CVE-A"})
    assert to_close == [] and to_update == []


# ---------------------------------------------------------------------------
# resolve_tickets — ticket closing decoupled from the allowlist
# ---------------------------------------------------------------------------


def test_medium_ticket_closes_without_any_allowlist_entry():
    # Regression for BLDX-1465: a Medium CVE ticket (never allowlisted) must
    # still close once the scanner stops seeing it for `debounce` scans.
    tickets = [_ticket("BLDX-1465", ["CVE-2026-2303"])]
    scans = [set(), set(), set()]  # gone in all 3 scans; no allowlist involved
    to_close, to_update = rec.resolve_tickets(tickets, scans, debounce=3)
    assert [t["identifier"] for t in to_close] == ["BLDX-1465"]
    assert to_update == []


def test_resolve_tickets_keeps_ticket_with_present_cve():
    tickets = [_ticket("BLDX-9", ["CVE-STILL"])]
    scans = [{"CVE-STILL"}, {"CVE-STILL"}, {"CVE-STILL"}]
    to_close, to_update = rec.resolve_tickets(tickets, scans, debounce=3)
    assert to_close == [] and to_update == []


def test_resolve_tickets_trims_marker_when_partially_resolved():
    tickets = [_ticket("BLDX-10", ["CVE-GONE", "CVE-STILL"])]
    scans = [{"CVE-STILL"}, {"CVE-STILL"}, {"CVE-STILL"}]  # CVE-GONE absent in all
    to_close, to_update = rec.resolve_tickets(tickets, scans, debounce=3)
    assert to_close == []
    assert to_update[0]["remaining_ids"] == ["CVE-STILL"]


def test_resolve_tickets_failsafe_with_thin_history():
    tickets = [_ticket("BLDX-11", ["CVE-X"])]
    scans = [set(), set()]  # only 2 scans of history (< debounce 3)
    to_close, to_update = rec.resolve_tickets(tickets, scans, debounce=3)
    assert to_close == [] and to_update == []


def test_resolve_tickets_closes_on_first_clean_scan_with_debounce_one():
    # Default behavior: a ticket closes as soon as its CVE is absent from the
    # latest scan (debounce=1) — no waiting for prior scans.
    tickets = [_ticket("BLDX-12", ["CVE-Y"])]
    to_close, _ = rec.resolve_tickets(tickets, [set()], debounce=1)
    assert [t["identifier"] for t in to_close] == ["BLDX-12"]


# ---------------------------------------------------------------------------
# defaults + close comment
# ---------------------------------------------------------------------------


def test_default_debounce_is_one():
    assert rec.DEFAULT_DEBOUNCE == 1


def test_close_comment_body_mentions_cves_and_run():
    body = rec.close_comment_body(["CVE-2026-2303"], "https://x/run/1")
    assert "Auto-resolved" in body
    assert "`CVE-2026-2303`" in body
    assert "vulnerability" in body  # singular for one CVE
    assert "https://x/run/1" in body


def test_close_comment_body_plural_and_no_run_url():
    body = rec.close_comment_body(["CVE-1", "CVE-2"], "")
    assert "vulnerabilities" in body  # plural for two
    assert "Reconcile run:" not in body  # omitted when no run URL


def test_resolution_phrase():
    assert rec._resolution_phrase(1) == "the latest clean scan"
    assert "3 consecutive" in rec._resolution_phrase(3)


# ---------------------------------------------------------------------------
# reconcile_tickets — side-effect wiring (close before comment; CVEs in body)
# ---------------------------------------------------------------------------


def _wire_linear(monkeypatch, tickets, *, close_ok=True):
    """Monkeypatch the Linear I/O reconcile_tickets depends on; return a call log."""
    calls: list[tuple] = []
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.setenv("LINEAR_TEAM_ID", "team-1")
    monkeypatch.setattr(rec.ssl, "resolve_label_id", lambda *a, **k: "label-1")
    monkeypatch.setattr(rec.ssl, "fetch_open_labelled_issues", lambda *a, **k: tickets)
    monkeypatch.setattr(rec, "resolve_done_state_id", lambda *a, **k: "done-state")

    def fake_close(issue_id, state_id):
        calls.append(("close", issue_id))
        return close_ok

    def fake_comment(issue_id, body):
        calls.append(("comment", issue_id, body))

    monkeypatch.setattr(rec, "close_ticket", fake_close)
    monkeypatch.setattr(rec, "comment_on_ticket", fake_comment)
    return calls


def test_reconcile_tickets_closes_then_comments_with_cves(monkeypatch):
    ticket = _ticket("BLDX-1", ["CVE-A", "CVE-B"])
    calls = _wire_linear(monkeypatch, [ticket])
    rec.reconcile_tickets([set()], debounce=1)  # both CVEs absent → resolved
    # Close runs before comment, each once.
    assert [c[0] for c in calls] == ["close", "comment"]
    body = calls[1][2]
    assert "CVE-A" in body and "CVE-B" in body


def test_reconcile_tickets_skips_comment_when_close_fails(monkeypatch):
    ticket = _ticket("BLDX-2", ["CVE-A"])
    calls = _wire_linear(monkeypatch, [ticket], close_ok=False)
    rec.reconcile_tickets([set()], debounce=1)
    assert [c[0] for c in calls] == ["close"]  # no comment after a failed close
