# Credential Leak Gate (centralized)

The `build-and-publish-app.yaml` reusable workflow runs a **`leak-scan`** job
before it publishes a version. The job **blocks the publish** when the app
contains a credential leak. Like the `certify` job, it needs **no per-app
wiring** — every app is scanned at publish time without editing its own
workflows.

It is the publish-time counterpart of the nightly **credential-leak-scan**
in `atlanhq/connector-pulse` (the APP-1602 audit pipeline). The nightly scan
is detection + tracking (Linear tickets, ClickHouse) and uses an LLM to triage
candidates. This gate is **deterministic** (no LLM, no token): same sink
pattern set and severity rubric, but it just renders a pass/fail verdict in the
publish path.

## What it checks

The job checks out the app source and runs two layers, both blocking, before
any build minutes are spent:

| Layer | Tool | Blocks on |
|-------|------|-----------|
| **Hardcoded secrets** | [`gitleaks`](https://github.com/gitleaks/gitleaks) over the working tree | any committed secret (API key, token, private key, …) |
| **Creds-into-logs** | `scan.py` regex scanner (`.github/scripts/credential-leak-gate/`) | any **CRITICAL/HIGH** credential reaching a log / print / format / CLI-arg sink |

`MEDIUM`/`LOW` creds-into-logs findings (e.g. `helm --set password=` in an e2e
script, or any finding under a `tests/` path) are reported in the job summary
but do **not** block.

## How blocking works

`leak-scan` runs only when `publish: true` (the same condition as
`validate-channel` and `certify`). It is in `prepare`'s `needs`, and `prepare`
gates on `!failure()` — so a finding in either layer trips `failure()` and the
whole `prepare → build → merge → publish` chain is skipped. The gate **fails
closed**: a missing or malformed scanner verdict also blocks.

## The creds-into-logs scanner

`scan.py` mirrors the canonical ruleset in
`.github/scripts/credential-leak-gate/credential-leak-rules.md` (vendored from
the connector-pulse nightly skill — keep the two in sync). It replaces the
nightly's LLM triage with static heuristics that keep the false-positive rate
low enough for a blocking gate:

- A candidate is dropped when the line routes the value through a masking
  helper (`mask`/`redact`/`***`/…).
- A candidate is dropped when the matched token is a **metadata identifier**
  (`credential_name`, `secret_id`, `credential_guid`, …) rather than the value.
- A candidate must be referenced **as code** (interpolated, passed as a
  variable, concatenated) — a credential word appearing only inside a log
  *message string* is not a leak.
- Findings under test/fixture paths and `helm --set` args cap at `MEDIUM`
  (non-blocking).

Secret values are never recorded or printed — the verdict and job summary
report only `file:line`, the pattern id, the matched identifier, and severity.

## Fixing a blocked publish

**Hardcoded secret (gitleaks):** remove the secret, **rotate** any value that
was committed, and re-run. Genuine false positives go in an app-level
`.gitleaks.toml` config (the gate passes `--config .gitleaks.toml` when present).

**Creds-into-logs (`scan.py`):** route the credential through the SDK redaction
helper before the sink, or drop the log line. Confirmed false positives go in
an app-level `.credential-leak-allow` file at the repo root:

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
