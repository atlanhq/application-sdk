"""vuln-triage: the deterministic CVE triage for one security-scan Linear ticket.

It replaces the mothership rover (.mothership/vuln-triage, retired). Every call that
rover made came from data already on disk, so this does the same work in plain code
with no model, no VPN and no sandbox. There is one exception. Case 2 vs Case 3 (no fix:
is upstream still maintained?) asks PyPI live. That verdict and its evidence (the newest
release date) are written to the ticket, so a re-run can be checked against them:

    ticket marker  ─┐
    Trivy JSON     ─┼─► classify (case 1-4 / killed) ─► allowlist PR (Critical/High)
    uv.lock        ─┘                                 ─► bump PR (Case 1)
                                                      ─► one Linear comment

Pure logic (scan parsing, classification, allowlist and bump planning, the report) is in
the modules below and unit-tested in .github/scripts/tests/test_vuln_triage.py. The I/O
(git, gh, Linear, PyPI, uv) is in effects.py, driven by run.py. The safety boundary is still
vuln_auto_merge_gate.py: it auto-merges only the two PR shapes, whatever this code does.
"""
