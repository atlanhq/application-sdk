# Security Allowlist

All CVE exceptions for the SDK base image are managed centrally in `base-allowlist.json` in **this repository**. Per-repo allowlists are not supported — connector repos must not create their own `.security/allowlist.json`.

## Adding an entry

Open a PR against `atlanhq/application-sdk` adding the CVE to `.security/base-allowlist.json`. The PR must be reviewed by the SDK security owner (see CODEOWNERS).

## Format

```json
{
  "CVE-2024-12345": {
    "package": "affected-package",
    "severity": "HIGH",
    "reason": "Base image — no fix available; mitigated by network policy.",
    "expires": "2025-06-01",
    "added_by": "@your-github-handle"
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `package` | yes | Affected package name |
| `severity` | yes | `CRITICAL` or `HIGH` |
| `reason` | yes | Why this CVE is acceptable (workaround, no fix, low exposure, etc.) |
| `expires` | yes | ISO date (`YYYY-MM-DD`). After this date the entry is treated as a new finding and blocks merge. |
| `added_by` | yes | GitHub handle of the person who approved the allowlist entry. |

## Rules

- Expiry dates are staggered by severity: CRITICAL → 60 days, HIGH → 90 days.
- Entries with a `_comment` key (or any key starting with `_`) are ignored by the gate.
- To renew an entry, update `expires` and confirm the CVE is still unresolvable.

## Gate behaviour

The Trivy gate in `.github/workflows/trivy-container.yaml` (and inline in `build-apps-image.yaml`) sparse-checks out `.security/` from `atlanhq/application-sdk@main` at runtime, so all connector repos always use the latest centralized allowlist without any local changes required.

| Finding state | Gate outcome |
|---|---|
| Not in base-allowlist | **Fails** (new vulnerability — add entry or fix the CVE) |
| In base-allowlist, not expired | Passes (listed as allowlisted) |
| In base-allowlist, expired | **Fails** (entry needs renewal or the CVE should be fixed) |
| base-allowlist unavailable | Findings reported but gate does not block |
