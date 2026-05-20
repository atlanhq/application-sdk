# v3 Security Review Rules

Combines Atlan AGENTS.md security invariants with v3-specific security patterns.

---

## Secret Management (Critical)

### No Secrets in Code

Flag ANY of these in the diff:
- Hardcoded API keys, tokens, passwords, connection strings
- Base64-encoded secrets
- Private keys or certificates in source
- `.env` files with real credentials committed
- Test fixtures with real credentials (must use mocks)

### No Secrets in Logs

Flag log statements that could leak:
- Credential objects logged without redaction
- Auth headers logged at any level
- Connection strings with embedded passwords
- API keys in debug output
- Session tokens, cookies, JWTs

```python
# BAD
logger.debug("Credentials: %s", credentials)
logger.info("Headers: %s", headers)  # may contain Authorization
logger.error("Connection failed: %s", connection_string)  # may contain password

# GOOD
logger.debug("Credential type: %s", type(credentials).__name__)
logger.info("Request to %s", url)  # URL without auth params
logger.error("Connection to %s:%d failed", host, port)
```

### Credential Handling

v3 uses typed `CredentialRef` → `Credential` pattern. Flag:
- Raw dict credential access (must use `CredentialRef` + `CredentialResolver`)
- Credentials stored in `app_state` (use `SecretStore` protocol)
- Credential values passed through Temporal payloads (use `CredentialRef` reference, not the value)
- Missing credential validation before use

---

## SQL Injection (Critical)

### Parameterized Queries Only

```python
# BAD — SQL injection
query = f"SELECT * FROM {table} WHERE id = '{user_input}'"
cursor.execute(f"SELECT * FROM tables WHERE schema = '{schema_name}'")

# GOOD
cursor.execute("SELECT * FROM tables WHERE schema = %s", (schema_name,))
```

### Dynamic Table/Column Names

If table or column names must be dynamic (e.g., from metadata extraction):
- Validate against an allowlist of known identifiers
- Use SQL identifier quoting (`sql.Identifier()` in psycopg2, backtick quoting)
- Never interpolate user-provided strings directly into SQL

---

## Input Validation (Important)

### System Boundaries

Validate all input at system boundaries:
- Handler HTTP inputs (auth, preflight, metadata requests)
- CLI argument parsing
- Environment variable parsing
- External API response data before processing

v3's Pydantic contracts handle much of this automatically. Flag cases where:
- Raw dict data from external sources bypasses contract validation
- `model_validate()` is not used for external data
- Missing error handling around contract validation

### Path Traversal

```python
# BAD
file_path = os.path.join(base_dir, user_provided_path)

# GOOD
file_path = os.path.join(base_dir, os.path.basename(user_provided_path))
# Or validate: assert not user_provided_path.startswith('..')
```

---

## Command Injection (Critical)

### No Shell Injection

```python
# BAD
os.system(f"process {filename}")
subprocess.run(f"tool --input {user_input}", shell=True)

# GOOD
subprocess.run(["tool", "--input", user_input], shell=False)
```

Flag:
- `os.system()` — always a violation
- `subprocess.run(..., shell=True)` with variable interpolation
- `eval()`, `exec()` — always a violation
- `pickle.loads()` on untrusted data
- `yaml.load()` without `Loader=yaml.SafeLoader` (use `yaml.safe_load()`)

---

## Deserialization Safety (Critical)

```python
# BAD
data = pickle.loads(untrusted_bytes)
data = yaml.load(content)  # unsafe default loader
obj = eval(serialized_string)

# GOOD
data = json.loads(content)  # safe
data = yaml.safe_load(content)  # safe
data = MyContract.model_validate_json(content)  # Pydantic — safe
```

---

## Multi-Tenant Isolation (Critical)

- `tenant_id` must come from authenticated context only — never from request body or URL params
- Storage paths must be scoped to tenant (no cross-tenant reads/writes)
- State store keys must include tenant scoping
- Credential resolution must be tenant-scoped

Flag:
- Storage operations without tenant prefix in the key
- State store operations where tenant_id comes from user input
- Any pattern where Tenant A could access Tenant B's data

---

## Async Security

### Blocking in Event Loop (Important)

Per ADR-0010: blocking calls stall the entire event loop, which can:
- Prevent heartbeats → Temporal kills the task → retry storm
- Block health checks → Kubernetes restarts the pod
- Starve other concurrent tasks

Flag:
- `requests.get/post` in async code (use `httpx.AsyncClient`)
- `time.sleep()` in async code (use `asyncio.sleep()`)
- File I/O without `run_in_thread()` for large files
- Database calls with sync drivers without `run_in_thread()`

### Thread Safety

- `run_in_thread()` code must not share mutable state with the async caller
- Thread-local storage must not be used for passing context to threaded code

---

## Dependency Security (Important)

### Pinned Dependencies

- GitHub Actions must use SHA-pinned versions (`uses: actions/checkout@SHA`)
- Docker base images must use specific version tags (not `latest`)
- Python dependencies should have upper bounds to prevent surprise upgrades

### No Unsafe Dependencies

Flag introduction of known-vulnerable patterns:
- `pyyaml` < 6.0 (arbitrary code execution via `yaml.load()`)
- Any dependency that requires `--trusted-host` or `--allow-insecure`

---

## CORS and Network (Important)

- No wildcard CORS (`Access-Control-Allow-Origin: *`) in production code
- No wildcard IAM policies
- No binding to `0.0.0.0` without explicit documentation of why
- Health endpoints should not expose sensitive information

---

## Error Information Disclosure (Important)

- Stack traces must not be returned in HTTP responses to clients
- Error messages to external callers must not reveal internal paths, SQL queries, or infrastructure details
- Use `HandlerError(message, http_status)` with user-safe messages

```python
# BAD
except Exception as e:
    return {"error": str(e)}  # leaks internal details

# GOOD
except Exception as e:
    logger.error("Internal error: %s", e, exc_info=True)
    raise HandlerError("An internal error occurred", http_status=500) from e
```

---

## Supply Chain (Important)

- Dockerfile `FROM` must reference specific versions, not `latest`
- `ghcr.io/atlanhq/application-sdk-main:3.0.0` — pinned
- CI workflows must pin action versions to SHAs
- No `curl | bash` patterns for installing tools in Dockerfiles

---

## Retro Learnings — 2026-05-20

> Each subsection below is a generalized rule. Concrete incidents that
> surfaced the rule are linked in `retro-2026-05-20.md` and prior
> entries in this section.

### Credential material and bulk data must use separate storage components (Critical)

Credentials (keytabs, certificates, private keys, OAuth refresh
tokens, kerberos configs, any file whose disclosure compromises
authentication) MUST be retrieved through a storage component that is
not also used for extracted customer data or workflow artifacts.
Sharing a component means one IAM misconfiguration, one over-broad
role, or one SSRF lapse exposes both secrets and data.

**Flag any change that:**
- Adds a new branch / code path to credential resolution
  (`application_sdk/credentials/*`, anything matching
  `resolve_credential*`, `load_secret*`, `fetch_keytab*`) that reads
  from a storage binding, object-store component, or Dapr state
  component already used elsewhere for non-credential data (extract
  outputs, metadata dumps, query logs, intermediate artifacts).
- Uses the same env-var-named binding (`DEPLOYMENT_OBJECT_STORE_NAME`,
  `OUTPUT_*`, anything wired to data flow) for secret retrieval.
- Reads files of types typically containing secrets (`*.pem`, `*.key`,
  `*.keytab`, `*.p12`, `*.jks`, `*credential*`, `*secret*`, `*token*`)
  through a binding component name that the same app uses for data.

**Expected pattern:** credential reads go through a dedicated secret-
store binding, a separate object-store binding with stricter IAM/KMS,
or the connector's `CredentialResolver`. If a PR argues that a shared
component is the only practical path (e.g. value-size limits in the
secret manager), the PR description MUST cite the separate IAM
boundary that protects the credential path — otherwise escalate to
design review.

**Severity:** Critical (SEC). Counts as a G1 guardrail violation when
the same component name appears in both credential and data code paths.

### Content compatibility at the edge gateway (Critical)

When code adds, modifies, or expands what gets sent in the body of an
outbound HTTP request to an Atlan-platform edge endpoint (anything
routed through the Atlan API gateway / WAF — `home.atlan.com`,
`*.atlan.com/api/service/`, marketplace endpoints, Heracles routes,
SDR ingest endpoints, etc.), the new content must be verified
compatible with edge sanitization plugins (XSS scrubbers, schema
validators, size limits). These plugins compare sanitized output to
input and reject the request if they differ — silently breaking any
caller whose payload contains characters that look like HTML, JS, or
template syntax.

**Flag any change that:**
- Adds a new field to an outbound JSON body, multipart form, or
  request payload sent to an Atlan-edge endpoint, where the value
  carries: connector form schemas, generated JSON/YAML, Jinja or
  templating syntax (`{% %}`, `{{ }}`, `${...}`), PEM-encoded keys
  (`-----BEGIN ...`), HTML-like or angle-bracketed placeholders
  (`<account-id>`, `<region>`), SQL templates, or any
  user-/codegen-produced string blob.
- Inlines such content as a raw JSON string rather than wrapping it
  (base64, multipart file part, gzipped blob, etc.).
- Adds a new outbound call to an edge route without identifying which
  WAF plugins protect that route and confirming the payload survives
  them.

**Expected pattern:** for any new request body field crossing the
edge gateway, the PR description must state one of:
(a) the route is on the WAF plugin allowlist (link the config),
(b) the content is base64-encoded / multipart-uploaded,
(c) the field has been tested end-to-end against the actual edge with
representative content.

**Severity:** Critical (SEC + integration). Block until evidence is
attached. This is a recurring failure mode — past incidents include
private-key-passphrase contents tripping XSS validation; future
incidents will look similar.

### Bypass paths must be keyed on server-verifiable facts, not caller input (Critical)

Any conditional in a CI workflow, service handler, or release flow
that skips a security check, scoping check, or release gate MUST be
keyed on a fact the server can verify independently — not on a value
the caller supplies.

**Flag any change that:**
- Adds an `if:` / `continue-on-error` / early-return condition in a
  GitHub Actions workflow keyed on a `workflow_call` input,
  `workflow_dispatch` input, or `client_payload` value that, when
  set, skips: tenant scoping, release-branch verification, tag-
  ancestry verification, or any security/compliance gate.
- References a `release_tag`, `version`, `ref`, or `tenant` input
  without a server-verifiable check that the value matches reality:
  - Tag ancestry: `git merge-base --is-ancestor "$TAG" origin/main`
  - Trigger ref: `github.ref == format('refs/tags/{0}', inputs.release_tag)`
  - Tenant identity: resolved from a SAML/OIDC claim, never from a
    request field.
- Treats whitespace, empty strings, or null values as "skip the check"
  (an attacker can always send empty input).

**Severity:** Critical (SEC). Same attack class as trusting
`tenant_id` from a request body.

### Validator coverage must be uniform across same-class fields (Critical)

When a Pydantic model has a class of string fields that all flow to
the same risky sink (SQL, shell, file path, log filter, regex
compilation), every field of that class on the model MUST use the
same validator — not a weaker per-field shortcut.

**Flag any change that:**
- Adds a string field to a model where sibling fields already use a
  named validator (e.g. `@field_validator('foo')` calling
  `validate_filter_no_sql_injection`, `validate_no_shell_meta`,
  `validate_path_no_traversal`, etc.), and the new field omits it.
- Substitutes `Field(pattern=…)` for a named validator when the named
  validator catches multi-character patterns (`--`, `/*`, `*/`,
  shell expansions, path traversals) that the regex doesn't.
- Adds a model with only some fields validated for the same sink
  class — e.g. two SQL-bound fields where one has the validator and
  the other doesn't.

**How to detect quickly:** grep the model file for `@field_validator`
and `validate_*` function calls. Any new string field on the same
class without one of these is a candidate finding.

**Severity:** Critical (SEC). Partial mitigation on an injection path
is functionally no mitigation.
