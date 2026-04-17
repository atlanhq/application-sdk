# Security Allowlist

`allowlist.json` records CVEs that have been reviewed and deliberately accepted for this repository. The Trivy security gate in `.github/workflows/trivy-container.yaml` consults this file to distinguish *new* findings from *known-and-approved* ones.

## Format

```json
{
  "CVE-2024-12345": {
    "reason": "No fix available; mitigated by network policy.",
    "expires": "2025-06-01",
    "owner": "@your-github-handle"
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `reason` | yes | Why this CVE is acceptable (workaround, no fix, low exposure, etc.) |
| `expires` | yes | ISO date (`YYYY-MM-DD`). After this date the entry is treated as a new finding and blocks merge. |
| `owner` | yes | GitHub handle of the person who approved the allowlist entry. |

## Rules

- Every entry **must** have an `expires` date no more than 90 days out.
- Entries with a `_comment` key are ignored by the gate (used for documentation).
- To renew an entry, update `expires` and confirm the CVE is still unresolvable.
- To add an entry, open a PR with the allowlist change reviewed by a security owner.

## Gate behaviour

| Finding state | Gate outcome |
|---|---|
| Not in allowlist | **Fails** (new vulnerability) |
| In allowlist, not expired | Passes (listed as allowlisted) |
| In allowlist, expired | **Fails** (entry needs renewal or fix) |
| No `allowlist.json` present | Findings reported but gate does not block |
