# Connector Conventions

Naming, shape, and path conventions the SDR test pipeline pins to.
Reference these when adapting the recipe to a connector that doesn't
match the single-credential default.

## agent_json shape

The test class's `agent_spec_template` is what `BaseSDRIntegrationTest._build_scenario_args` injects on workflow scenarios. It's the **structured agent specification** the SDK's `CredentialResolver` reads when `extraction_method=agent`.

```python
_AGENT_JSON: Dict[str, Any] = {
    "agent-name": "<app>-ci-agent",       # arbitrary label, surfaces in observability
    "secret-manager": "local",            # SDR uses local.file Dapr secret store
    "secret-path": "<app>-credentials",   # MUST match make-secrets.py's top-level key
    "auth-type": "basic",                 # or "oauth", "iam", per connector
    "host": _app_host,                    # plain values for non-secret fields
    "port": _app_port,
    "basic.username": "username",         # ref-keys — the SDK substitutes these from secret-store
    "basic.password": "password",         # by looking up `<secret-path>.username` etc.
}
```

The values for `basic.username` / `basic.password` are **ref-keys, not literal credentials**. They name the keys inside the stored bundle. The SDK's `AgentCredentialSpec.resolve()` does:

```
secret_store.get(secret_path) → JSON-parse → bundle dict
credentials.username = bundle[ref_key("basic.username")]    # = bundle["username"]
credentials.password = bundle[ref_key("basic.password")]    # = bundle["password"]
```

If you change `make-secrets.py` to write `client_id`/`client_secret` as the bundle keys instead of `username`/`password`, update `agent_spec_template` to match:

```python
"basic.username": "client_id",
"basic.password": "client_secret",
```

## secret-store path

The composite mounts `.github/e2e/secrets/credentials.json` at `/app/secrets/credentials.json` inside the container. The Dapr `local.file` secretstore reads from this path. Don't change either side — both are pinned by the composite + `docker-compose.ci.yml`.

If your connector needs a *different* secret-store path (e.g. it has multiple credential bundles for different sub-systems), override via:

- `make-secrets.py` writes additional top-level keys (e.g. `looker-git-credentials`, `looker-oauth-credentials`)
- Per-scenario `agent_spec_template` overrides on workflow scenarios that need the alternative bundle

## E2E env-var prefix

The framework's `BaseIntegrationTest._discover_credentials_from_env` strips `E2E_<APP_UPPER>_` and lower-snake-cases the rest:

| Env var | `app-name: foo` | `app-name: foo_bar` |
|---|---|---|
| `E2E_FOO_HOST` | `host` ✓ | (skipped — no `_BAR_` prefix) |
| `E2E_FOO_BAR_HOST` | `bar_host` ✗ | `host` ✓ |
| `E2E_FOOBAR_HOST` | (skipped) | (skipped) |

**Rule:** match the env-var prefix exactly to your `app-name` input — uppercase, no extra suffix, no missing components.

## Multi-credential connectors

Connectors that need multiple credential bundles (SAP ECC + S4, IAM-auth + basic-auth, etc.) deviate from the default in three places:

1. **`make-secrets.py`** writes multiple top-level keys:
   ```python
   out = {
       "saperp-ecc-credentials": json.dumps(ecc_bundle),
       "saperp-s4-credentials": json.dumps(s4_bundle),
   }
   ```
2. **Test class** declares per-scenario `agent_spec_template` overrides via either:
   - Multiple test classes (one per bundle), each with a different class-level `agent_spec_template`
   - Per-scenario `agent_spec` field on `Scenario(...)` if the framework supports it
3. **Caller workflow** sets the corresponding env vars (`E2E_SAPERP_ECC_*`, `E2E_SAPERP_S4_*`)

See `atlan-saperp-app/tests/sdr/test_saperp_sdr.py` for the canonical multi-credential implementation.

## REST connectors with extras

Connectors with non-credential extras (FLL git creds, custom HTTP headers, etc.) attach them to the `extra` field on the credentials dict. The bundle written to credentials.json:

```python
bundle = {
    "username": os.environ["E2E_FOO_USERNAME"],
    "password": os.environ["E2E_FOO_PASSWORD"],
    "extra": {
        "git_token": os.environ.get("E2E_FOO_GIT_TOKEN", ""),
        "use-field-level-lineage": "true",
    },
}
```

The connector's `build_credentials` or handler reads `extra` directly. **Note:** the Dapr local.file secretstore with `multiValued: true` flattens nested dicts into colon-separated top-level keys (`extra:git_token`). If your connector reads `credentials["extra"]["git_token"]`, add an unflatten step at the resolution boundary, or set `multiValued: false` and store nested dicts as JSON strings (the recommended pattern — see `make-secrets.py` skeleton in SKILL.md).

## TestSuite path

Place the SDR suite at `tests/sdr/test_<connector>_sdr.py`. The composite's default `test-path` input is `tests/sdr/`; pytest discovers any `test_*_sdr.py` under that. If you need a different layout, override via `with: test-path:` on the composite invocation.

## Output path

The mounted `./data` volume on the host maps to `/data/storage` inside the container (the Dapr `objectstore` `bindings.localstorage` rootPath). Workflow output lands at:

```
./data/artifacts/apps/<app>/workflows/<workflow_id>/<run_id>/{raw,filtered,transformed,...}/
```

The workflow scenario's `extracted_output_base_path` should be `data/artifacts/apps/<app>/workflows`. The base class's `_validate_extracted_output` will scan this path post-completion.

## Tenant / OAuth secrets

The `SDR_*` triple authenticates the *worker* against the *Atlan tenant's* IDP, not the source system. They're orthogonal to the connector's API credentials.

| Secret | Format | Source |
|---|---|---|
| `SDR_TEST_TENANT` | bare domain (e.g. `e2e-aws.atlan.com`) | Tenant config |
| `SDR_CLIENT_ID` | `oauth-client-<uuid>` | Atlan tenant Keycloak (Service Accounts → look for an existing `apps-typedef`-scoped client, or create one) |
| `SDR_CLIENT_SECRET` | UUID | Same Keycloak client |

Existing connectors with passing SDR pipelines (mssql, saperp) have these set as **org-level** secrets so new connectors auto-inherit. If your repo doesn't see them auto-populated, set as repo-level secrets per the recipe in SKILL.md.
