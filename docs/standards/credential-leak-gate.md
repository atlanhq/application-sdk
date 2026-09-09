# Credential Leak Gate (centralized)

The `build-and-publish-app.yaml` reusable workflow runs a **`leak-scan`** job
before it publishes a version. Like the `certify` job, it needs **no per-app
wiring** — every app is scanned at publish time without editing its own
workflows.

It has **two layers with different postures**, chosen from a measured dry run
against all 152 `atlan-*-app` repos:

- **Hardcoded secrets (gitleaks) — BLOCKS the publish.** High precision.
- **Creds-into-logs (regex scanner) — WARN-ONLY.** Pure-regex detection has an
  irreducible false-positive rate (it cannot always tell a logged *secret
  value* from a logged *non-secret field* of a credential object), so it never
  blocks: it annotates the run summary and pings a Slack channel when there are
  warnings. Blocking it outright would freeze ~7 repos on false positives.

It is the publish-time counterpart of the nightly **credential-leak-scan**
in `atlanhq/connector-pulse` (the APP-1602 audit pipeline). The nightly scan
is detection + tracking (Linear tickets, ClickHouse) and uses an LLM to triage
candidates. This gate is **deterministic** (no LLM, no token): same sink
pattern set and severity rubric, but it just renders a pass/fail verdict in the
publish path.

## What it checks

The job checks out the app source and runs two layers before any build minutes
are spent:

| Layer | Tool | Posture | On finding |
|-------|------|---------|------------|
| **Hardcoded secrets** | [`gitleaks`](https://github.com/gitleaks/gitleaks) over the shipped source | **Blocking** | any committed secret (API key, token, private key, …) **fails the publish** |
| **Creds-into-logs** | `scan.py` regex scanner (`.github/scripts/credential-leak-gate/`) | **Warn-only** | CRITICAL/HIGH findings annotate the run summary + ping Slack; publish proceeds |

`MEDIUM`/`LOW` creds-into-logs findings (e.g. `helm --set password=` in an e2e
script, or anything under a `tests/` path) are counted but not surfaced as
warnings.

### What the gitleaks layer does not scan

The **gitleaks layer** scans a pruned copy of the checkout with `tests/`,
`test/` and `.github/` removed (FND-678). Nothing under those paths can block a
publish. The copy matters: the warn-only creds-into-logs layer still scans the
full `app/` tree afterwards, so this narrowing applies to the blocking layer
only.

Hermetic fixtures are *built out of* credential-shaped strings, and a
value-shaped detector cannot tell a generated golden dossier of fake API keys
from a leak: one such file blocked `atlan-microstrategy-app`'s publish with 148
`generic-api-key` findings. The alternative — every app carrying a hand-written
`.gitleaks.toml`, extended one false-positive shape at a time — puts the fleet's
release path behind a config schema that **fails silently** on the pinned
gitleaks 8.21.2 (see FND-679: the plural `[[allowlists]]` form is parsed and
ignored, with no warning and no non-zero exit).

This is a deliberate **narrowing of the gate**, not a false-positive filter. Be
explicit about the cost: a real credential committed under `tests/` or
`.github/` is no longer reported by this gate. Measured on the pinned 8.21.2
against a synthetic tree, pruning took a scan from 152 findings to 2 — it
dropped 148 fixture false positives *and* 2 genuine planted tokens that lived
under those paths.

What still covers those paths:

- **The creds-into-logs layer in this same job** — it scans `--root app`, the
  unpruned tree, and is unaffected.
- **`S001 HardcodedCredential` / `S002 RawEnvCredentialAccess`** (conformance
  S-series) read test sources and are not affected by this pruning.
- **The nightly `credential-leak-scan`** in `atlanhq/connector-pulse` still
  walks the whole tree, including tests and CI wiring.

Pruning a copy rather than allowlisting is what makes this immune to gitleaks
config-schema skew, and means it needs no merge with an app-level
`.gitleaks.toml`.

## How blocking works

`leak-scan` runs only when `publish: true` (the same condition as
`validate-channel` and `certify`). It is in `prepare`'s `needs`, and `prepare`
gates on `!failure()`.

**Only the gitleaks layer blocks.** A committed secret fails the `leak-scan`
job, which trips `failure()` and skips the whole `prepare → build → merge →
publish` chain. The creds-into-logs layer is warn-only — it never fails the
job, so its findings (and any scanner error) do not block the publish.

## Slack alerts

If the optional `SLACK_LEAK_SCAN_WEBHOOK` secret is set, the job pings Slack in
**two** cases (locations only — never secret values):

| Trigger | Message | Publish |
|---------|---------|---------|
| gitleaks finds a hardcoded secret | `:no_entry: Publish BLOCKED — hardcoded secret(s)` | **blocked** |
| creds-into-logs finds CRITICAL/HIGH | `:warning: Credential-leak warnings (publish NOT blocked)` | proceeds |

When the secret is unset, both Slack steps skip silently.

## Warnings: run summary + Slack

When the creds-into-logs scan produces CRITICAL/HIGH findings:

- A **run-summary** section lists the finding **locations** (`file:line`,
  pattern id, identifier, severity) — never secret values.
- If the optional **`SLACK_LEAK_SCAN_WEBHOOK`** secret is set (a
  `hooks.slack.com` incoming-webhook URL), the job posts the same location-only
  summary to that channel with a link to the run. When unset, the Slack step is
  skipped silently.

  Set it **once at the org level** and let apps pick it up via `secrets:
  inherit` — the same convention as `SLACK_DEP_COOLDOWN_WEBHOOK`
  (`atlanhq/.github` → `reusable-dep-cooldown.yml`). There is no shared generic
  Slack secret in the org; each pipeline uses its own `SLACK_<purpose>_WEBHOOK`.
  The webhook URL itself encodes the destination channel. The step refuses to
  POST anywhere other than `hooks.slack.com`.

  **Destination channel:** `#temp-int-studio-test` (`C0ALP5HSTCG`) — create the
  incoming webhook against this channel and store its URL as the org secret.
  (A test channel suits the current warn-only rollout; repoint the webhook to a
  permanent channel later without any workflow change.)

## The creds-into-logs scanner

`scan.py` mirrors the canonical ruleset in
`.github/scripts/credential-leak-gate/credential-leak-rules.md` (vendored from
the connector-pulse nightly skill — keep the two in sync). It replaces the
nightly's LLM triage with static heuristics that keep the warning signal clean:

- A candidate is dropped when the line routes the value through a masking
  helper (`mask`/`redact`/`***`/…).
- A candidate is dropped when the matched token is a **metadata identifier**
  (`credential_name`, `secret_id`, `credential_guid`, …) rather than the value.
- A candidate must be referenced **as code** (interpolated, passed as a
  variable, concatenated) — a credential word appearing only inside a log
  *message string* is not a leak.
- A credential accessed by a known **non-secret field** (`creds['host']`,
  `creds.get('authType')`, `creds.account`, …) logs that field, not the secret.
- Findings under test/fixture paths and `helm --set` args cap at `MEDIUM`.
- Security rule-definition files (`*.semgrep.yml`) are skipped — they contain
  sink patterns as *rules*, not leaks.

Secret values are never recorded or printed — the verdict, run summary, and
Slack message report only `file:line`, the pattern id, the matched identifier,
and severity.

## Acting on a finding

**Hardcoded secret (gitleaks) — blocks:** remove the secret, **rotate** any
value that was committed, and re-run. Because `tests/`, `test/` and `.github/`
are not scanned at all, a finding here is in **shipped application source** —
treat it as real before reaching for an allowlist.

For a genuine false positive in shipped source, an app-level `.gitleaks.toml`
is still honoured (the gate passes `--config .gitleaks.toml` when present).
Write it as a **single singular `[allowlist]` table** with `regexTarget =
"line"`: the pinned 8.21.2 silently ignores the plural `[[allowlists]]` array
form, so a config written that way validates against a modern local binary and
does nothing in CI (FND-679). Prefer scoping an entry by the *value shape* over
the file path, so a real secret added to the same file is still caught:

```toml
[extend]
useDefault = true

[allowlist]
description = "Well-known local-emulator constant, grants access to nothing"
regexTarget = "line"
regexes = [
  '''AccountName=devstoreaccount1''',
]
```

**Creds-into-logs (`scan.py`) — warning, does not block:** route the credential
through the SDK redaction helper before the sink, or drop the log line.
Confirmed false positives go in an app-level `.credential-leak-allow` file at
the repo root (this keeps the warning + Slack noise down):

```
# .credential-leak-allow — one entry per line
path/to/file.py            # ignore the whole file
path/to/other.py:123       # ignore a single line
```

## Relationship to the other security jobs

- **`security-scan`** (`build-and-scan.yaml`, Trivy) scans the built **image**
  for CVEs. It is about vulnerable dependencies, not leaked credentials.
- **`leak-scan`** scans the **source** for leaked/hardcoded credentials.
- **Nightly credential-leak-scan** (connector-pulse) is the wider,
  LLM-assisted detection + Linear-tracking pipeline. This gate is the
  synchronous, deterministic enforcement point at publish time.
