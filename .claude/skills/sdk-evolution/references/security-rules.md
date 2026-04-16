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

v3 uses typed `CredentialRef` -> `Credential` pattern. Flag:
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
- Prevent heartbeats -> Temporal kills the task -> retry storm
- Block health checks -> Kubernetes restarts the pod
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

- **GitHub Actions:** Flag only truly mutable refs: `@main`, `@latest`, `@master`. Reusable workflows referenced at `@main` with `secrets: inherit` are HIGH severity. **Do NOT flag major version tags (`@v4`, `@v3`) on official GitHub/Docker-maintained actions** (`actions/*`, `docker/*`, `github/*`) — these are the vendor-recommended pinning level and are extremely low risk. Only flag major-only tags on third-party/unknown action authors. Full semver tags like `@v6.0.2` are ideal but not required for official actions.

*Lesson from 2026-04-16 run: PR #1408 was closed because pinning official Docker actions from @v3→@v4.1.0 and @v5→@v7.1.0 introduced breaking-change risk across a reusable workflow used by all app repos, with no real security benefit over the existing major version tags.*
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
