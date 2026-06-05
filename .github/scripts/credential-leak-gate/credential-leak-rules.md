# Credential Leak Rules (publish-gate copy)

Reference ruleset for the **publish-time** credential-leak gate. The patterns
below are implemented deterministically (no LLM) by `scan.py` in this
directory, driven by the `leak-scan` job in
`.github/workflows/build-and-publish-app.yaml`.

> **Provenance / sync.** This ruleset is vendored from
> `atlanhq/connector-pulse` →
> `skills/credential-leak-scan/references/credential-leak-rules.md`,
> the canonical source maintained by the nightly credential-leak scan.
> The nightly scan is detection + tracking (Linear tickets, ClickHouse) and
> uses an LLM to triage candidates; this copy drives the **publish-time gate**
> — same sink pattern set and severity rubric, but **deterministic** (the LLM
> triage/adversarial stages are replaced by static heuristics, see below) and
> **warn-only** (creds-into-logs findings annotate the run + ping Slack; they do
> not block — only the gitleaks hardcoded-secret layer blocks). **Keep the
> pattern set + severity rubric in sync** when either side changes. The pattern
> IDs are stable — do not rename them.

> **Deterministic gate notes (how `scan.py` diverges from the nightly):**
> - **`fstring-interp` is folded into the sinks, not standalone.** An f-string
>   interpolating a credential is only a leak when it reaches a log/print/CLI
>   sink; the `logger-call`/`print-call`/`console-call`/`go-fmt-call` patterns
>   already match the interpolated form via `.*{cred}`. A standalone f-string
>   pattern flags benign value construction (e.g. building a basic-auth header).
> - **Static triage replaces the LLM.** A candidate is dropped when: the line
>   routes the value through a masking helper (`mask`/`redact`/`***`/…); the
>   matched token is a metadata identifier (`credential_name`, `secret_id`,
>   `credential_guid`, …); or the credential identifier appears only inside a
>   message string literal, never referenced as code.
> - **Non-secret field access.** Logging `creds['host']` / `creds.get('authType')`
>   / `creds.account` reveals connection metadata, not the secret value.
> - **Severity (warn-only).** CRITICAL/HIGH raise a warning + Slack ping;
>   MEDIUM/LOW are counted only. `helm-set` and test/fixture paths cap at MEDIUM.
>   Nothing here blocks the publish — only the gitleaks layer does.
> - **Allowlist.** An app-level `.credential-leak-allow` file suppresses
>   confirmed false positives (`path` to ignore a file, `path:lineno` for a
>   single line).

---

## File-extension allowlist (Stage 1)

Source files that commonly carry log/print/format/CLI-arg calls:

```
.py .go .ts .tsx .js .jsx .java .kt .rs .sh .yaml .yml
```

Extensions intentionally excluded (high false-positive rate, low value):
`.md`, `.rst`, `.txt`, `.json`, `.lock`, `.sum`, `.mod`, `.toml` (except
CI/workflow YAML which we keep).

---

## Credential identifier tokens

A candidate line must contain one of these identifiers (case-insensitive),
captured as the `variable_name` regex group for later triage:

```
password | passwd | pwd
secret   | client_secret | api_key | access_key | secret_key | private_key
token    | atlan_token  | atlan_api_token | bearer
authorization | credential | connection_string
```

Regex class (exact form used in both the nightly skill and this gate):

```regex
(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|atlan[_-]?token|atlan[_-]?api[_-]?token|bearer|authorization|credential|connection[_-]?string|private[_-]?key)
```

---

## Sink patterns (Stage 1)

Each pattern ID routes a credential identifier into an output / log / CLI-arg
sink. The IDs are stable — keep them unchanged so dedup across the nightly
scan and this gate stays aligned.

| Pattern ID | Language / Sink | Regex |
|------------|-----------------|-------|
| `logger-call` | Python/Java loggers: `logger.info(...)`, `logging.warning(...)` | `(log(ger)?\|logging)\.(debug\|info\|warn(ing)?\|error\|critical\|fatal\|trace)\s*\(.*{cred}` |
| `print-call` | Bare `print(...)` with credential | `(^\|[^A-Za-z0-9_.])print\s*\(.*{cred}` |
| `console-call` | JS/TS: `console.log`, `console.error`, etc. | `console\.(log\|info\|debug\|warn\|error)\s*\(.*{cred}` |
| `go-fmt-call` | Go: `fmt.Printf`, `log.Errorf`, etc. | `(fmt\|log)\.(Print\|Println\|Printf\|Errorf\|Fatalf\|Panicf)\s*\(.*{cred}` |
| `rust-println` | Rust: `println!`, `eprintln!`, `log::info!` | `(println!\|eprintln!\|log::(debug\|info\|warn\|error)!)\s*\(.*{cred}` |
| `fstring-interp` | Python f-string / JS template literal interpolating a credential var | `f["\'][^"\']*\{[^}]*{cred}[^}]*\}` |
| `shell-echo` | Shell `echo`/`printf` with credential env var | `(echo\|printf)\s.*\$\{?[A-Z_]*{cred}[A-Z_]*\}?` |
| `helm-set` | Helm/argo/kubectl CLI args: `--set password=...`, `--from-literal=password=...` | `(--set[= ]\|--from-literal[= ])\S*{cred}\S*=` |

`{cred}` above is the credential identifier regex class.

---

## Comment stripping

Before pattern matching, strip trailing inline comments so a literal word in a
comment doesn't fire:

- `#` — Python, shell, YAML, Ruby
- `//` — Go, JS, TS, Java, Kotlin, Rust

Block comments (`/* ... */`, `"""..."""`) are out of scope — rare in practice
and not worth the parser complexity.

---

## Severity rubric (Stage 2 — triage)

Applied only when the triage verdict is `LEAK`.

| Severity | Definition |
|----------|------------|
| CRITICAL | Full credential (password, secret_key, client_secret, atlan_token, atlan_api_token) reaches a sink in production code paths. |
| HIGH | Plaintext credential in TLS/URI config logged at non-production code paths (test scripts, dev-mode), OR partial credential (first N chars of token) logged anywhere. |
| MEDIUM | Credential passed as CLI `--set` arg in test/e2e scripts, OR credential logged behind a feature flag / debug log. |
| LOW | Credential-adjacent identifier reaches a sink but the value is structurally non-sensitive (e.g., a redacted URI format template where the credential is always masked). |

**Publish-gate warning threshold:** `CRITICAL` and `HIGH` raise a non-blocking
warning (run summary + Slack). `MEDIUM`/`LOW` are counted in the verdict only.
The creds-into-logs layer never blocks the publish — only the gitleaks
hardcoded-secret layer does.

---

## Known false-positive classes

Triage (Stage 2) and gate (Stage 3) should recognise these and classify as
`FALSE_POSITIVE`:

1. **Test fixtures** — `password = "dummy"`, `atlan_token = "test-token"`. Fixture files (`tests/`, `fixtures/`, `*_test.go`) passed to a logger are low value; classify LOW at most unless the fixture value itself is a real credential committed to the repo (in which case raise to HIGH as a separate secret-in-code finding — gitleaks should already catch that).
2. **Type annotations / signatures** — `def send(password: str)` does not route the value to a sink. Match only if the identifier appears inside a sink call, which the regex patterns above ensure.
3. **Docstrings / block comments** — out of scope per comment-stripping note.
4. **Already-redacted values** — a variable reassigned to `***` or passed through a `redact()` / `mask()` helper before reaching the sink. Triage must inspect the ±10-line context to spot this.
5. **Format template literals without interpolation** — `logger.info("password=%s", mask(p))` where the sink receives a masked arg. The triage prompt should favour the mask wrapper over the identifier name.

---

## Previously-confirmed leak patterns (APP-1602 audit)

Concrete patterns confirmed in the Apr 16, 2026 audit. Use them as ground-truth
examples in prompt engineering and as regression checks for this gate.

| Pattern | Sink |
|---------|------|
| `logger.info(f"credentials={credentials}")` — dict with password + atlan_token | `logger-call`, `fstring-interp` |
| Full cloud credentials (`client_secret`, `tenant_id`) passed to logger in activities/handlers | `logger-call` |
| `logger.info(f"access_key={aws_access_key} secret_key={...} token={atlan_api_token}")` | `fstring-interp`, `logger-call` |
| Broken URI redaction leaks plaintext password; TLS cert key password logged | `logger-call` |
| First N chars of an OAuth token logged (`logger.info("token: " + token[:20])`) | `logger-call` |
| Passwords passed as plaintext Helm `--set` CLI args in e2e tests | `helm-set` |
| Connection string with password logged in a test script | `logger-call` |
