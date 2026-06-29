# SDR Integration Tests — Troubleshooting

Catalogue of failure modes seen during first runs of the SDR e2e
pipeline on Looker / MSSQL / SAP, with the diagnostic step that
identifies each and the fix that gets the run green. Walk this list
top-to-bottom — earlier items mask later ones.

## 1. `app.yaml: No such file or directory`

```
##[group]Run envsubst < app.yaml > app-resolved.yaml
/home/runner/work/_temp/.../sh: line 1: app.yaml: No such file or directory
##[error]Process completed with exit code 1.
```

**Cause.** The composite action's "Resolve app.yaml" step does `envsubst < app.yaml > app-resolved.yaml` from the workspace root. Many connectors use the older `atlan.yaml` filename for everything else.

**Fix.** Commit a 3-line `app.yaml` alongside `atlan.yaml`:

```yaml
app_name: <app>
app_image: ${APP_IMAGE}
app_port: 8000
```

**Don't.** Try to `cp atlan.yaml app.yaml` in a workflow step before the composite — the SDK action does its own `actions/checkout`, which wipes any runtime-staged file.

## 2. `Error loading app configuration: app name is required`

```
Error loading app configuration: app name is required
##[error]Process completed with exit code 1.
```

**Cause.** `app.yaml` exists but has the *full* `atlan.yaml` shape (top-level key `name:`). atlan-configurator expects the configurator-input shape with `app_name:`.

**Fix.** Use the 3-line shape from above. **Don't** copy `atlan.yaml` into `app.yaml`.

## 3. `secrets-script not found at .github/e2e/make-secrets.py`

**Cause.** Either the script doesn't exist or the path in the workflow's `with: secrets-script:` doesn't match.

**Fix.** Confirm `ls .github/e2e/make-secrets.py` and that the workflow yaml's `with:` block points at the same path. The composite invokes `python3 ${SCRIPT}` (so a Python script is required, not a shell script — for shell, set `secrets-script: .github/e2e/make-secrets.sh` and write a bash script).

## 4. `secrets-script ran but did not produce .github/e2e/secrets/credentials.json`

**Cause.** Script ran but wrote to the wrong path or didn't `os.makedirs` the parent.

**Fix.** Use this skeleton — the composite is hardcoded to look at `.github/e2e/secrets/credentials.json`:

```python
os.makedirs(".github/e2e/secrets", exist_ok=True)
with open(".github/e2e/secrets/credentials.json", "w") as f:
    json.dump(out, f)
```

## 5. `CI_TENANT_DOMAIN is not set`

**Cause.** Caller workflow doesn't export the tenant + OAuth env vars at job level.

**Fix.** Add to the job's `env:` block:

```yaml
env:
  CI_TENANT_DOMAIN: ${{ secrets.SDR_TEST_TENANT }}
  CI_CLIENT_ID:    ${{ secrets.SDR_CLIENT_ID }}
  CI_CLIENT_SECRET: ${{ secrets.SDR_CLIENT_SECRET }}
```

The composite reads these at the docker-compose env-var bridge layer — they MUST be at job level (or step level on the composite step), not just on a child step.

## 6. `Failed to resolve TEMPORAL_HOST=... or AUTH_HOST=...`

**Cause.** DNS resolution failure for the tenant domain on the runner.

**Fix.** Verify `SDR_TEST_TENANT` is the bare domain (e.g. `e2e-aws.atlan.com`), not a URL with scheme. Most tenant URLs work; if a specific domain consistently fails, the runner's IPv6 resolution may be picking unreachable AAAA records — `extra_hosts:` in `docker-compose.ci.yml` to pin to IPv4 is the workaround.

## 7. `registry.atlan.com 504 Gateway Timeout`

```
ERROR: failed to build: failed to solve: unexpected status from HEAD request to
       https://registry.atlan.com/v2/public/app-runtime-base/manifests/3.5.0:
       504 Gateway Time-out
```

**Cause.** Transient infra failure pulling the base image. Cascades into `uv: command not found` because `setup-deps` then gets skipped.

**Fix.** Re-trigger by removing and re-adding the `sdr-e2e-test` label. If it persists across multiple attempts, check `#bu-platform-eng` for the registry status.

## 8. `httpx.HTTPStatusError: 401 Unauthorized` for `/auth/realms/default/protocol/openid-connect/token`

```
atlan-app-1 | httpx.HTTPStatusError: Client error '401 Unauthorized' for url
              'https://<tenant>/auth/realms/default/protocol/openid-connect/token'
```

**Cause.** `SDR_CLIENT_ID` / `SDR_CLIENT_SECRET` are wrong — typically because the connector's API credentials (e.g. Looker API3 client_id `w77D6DcNsd7JQcSHWr5G`) were used. Those don't authenticate to the Atlan tenant's Keycloak.

**Fix.** Use an **Atlan tenant OAuth client** (format `oauth-client-<uuid>` with a UUID secret) registered in the test tenant. Existing connectors with working SDR pipelines (mssql, saperp) already have valid creds at the org-secret tier — check there before requesting new ones.

The container will keep retrying token exchange until startup times out; symptom in CI is "SDR container failed to start within 60s" with the 401 buried in the logs. Increasing `container-health-timeout-seconds` doesn't help; the auth itself is failing.

## 9. `Required auth credentials not found.` despite valid env vars

```
E   AssertionError: Assertions failed for scenario 'auth_valid_credentials':
E     - success: expected equals(True), got False
E     - message: expected contains('Authentication'), got '... Required auth credentials not found.'
```

**Cause.** Env-var prefix mismatch. The framework's auto-discovery strips `E2E_<APP_UPPER>_` based on the `app-name` input to the composite. If `app-name: foo` and env vars are named `E2E_FOO_BAR_HOST`, the discovered field becomes `bar_host` (not `host`) and the connector handler doesn't recognise it.

**Fix.** Env var prefix must be **exactly** `E2E_<UPPERCASED_APP_NAME>_<FIELD>`:

| `app-name:` | Expected env var |
|---|---|
| `mssql` | `E2E_MSSQL_HOST` |
| `looker` | `E2E_LOOKER_HOST` |
| `saperp` | `E2E_SAPERP_HOST` |

**Common mistake.** Reusing the direct integration suite's `E2E_LOOKER_BASIC_*` envs (set up for `ATLAN_APPLICATION_NAME=looker_basic`) when the SDR action's `app-name: looker`. Either rename the env vars in the workflow yaml or change the `app-name` input — but pick one and align everything.

**Verify what was discovered.** Pytest's captured stdout in the failed run shows:

```
[INFO] application_sdk.testing.integration.runner -
       Auto-discovered N credential fields from E2E_<APP>_* env vars: ['<list>']
```

If the list contains `basic_host` instead of `host`, that's the smoking gun.

## 10. `[AAF-STR-004] local_path does not exist or is not a file/directory`

```
[AAF-STR-004] local_path does not exist or is not a file/directory:
              artifacts/apps/<app>/workflows/<wf>/<run>
```

**Cause.** Connector is calling `self.upload_to_atlan(UploadInput(output_path=...))` with an *object-store key* (typically the result of `get_object_store_prefix(output_path)`). The deprecated upload_to_atlan thunk forwards this straight to `App.upload(local_path=...)`, which interprets it as a real fs path → relative path doesn't resolve under the container's CWD.

**Fix.** Pass the connector's actual `output_path` (under `TEMPORARY_PATH`) directly:

```python
# Wrong:
migration_prefix = get_object_store_prefix(output_path)
await self.upload_to_atlan(UploadInput(output_path=migration_prefix))

# Right:
await self.upload_to_atlan(UploadInput(output_path=output_path))
```

`get_object_store_prefix` was useful when uploading to a separate object store via Dapr binding; the deprecated upload_to_atlan path is local→remote mirror and needs the real path. Mirrors `atlan-mssql-app/app/extractor.py`.

## 11. Workflow scenario passes but PR comment has no Temporal link

**Cause.** Either the test class extends `BaseIntegrationTest` (not `BaseSDRIntegrationTest`), OR `workflow_timeout` is 0 / unset.

**Fix.** Confirm:

```python
from application_sdk.testing.sdr import BaseSDRIntegrationTest

class TestFooSdr(BaseSDRIntegrationTest):  # not BaseIntegrationTest
    ...
```

And every workflow scenario sets a real timeout:

```python
Scenario(
    api="workflow",
    workflow_timeout=600,    # 0 short-circuits — scenario passes the moment start_workflow returns
    polling_interval=15,
    ...
)
```

## 12. SDR run ID has no `-extract` suffix; downstream publish workflow can't find artifacts

**Cause.** SDR mode dispatches the workflow with the Temporal-generated workflow ID (e.g. `LookerMetadataExtractionWorkflow-<hash>-<hash>`). The connector's older code might assume a `<wf>-extract/<run>/` prefix.

**Fix.** This is a separate code-side cleanup, not an SDR-test issue. Track separately. The SDR e2e suite validates the artifacts under whatever path the connector wrote — adjust `extracted_output_base_path` on the workflow scenario if the layout differs.

## 13. Re-trigger by label-toggle is no-op

**Cause.** GitHub Actions deduplicates concurrent triggers via the workflow's `concurrency: group:`. Removing and re-adding the label faster than the previous run completes lands as a skipped run.

**Fix.** Wait for the previous run to leave `in_progress` state before re-toggling. Or use `workflow_dispatch` from the Actions tab — that bypasses the dedup.

## Diagnostic playbook

When a run fails, walk this in order:

1. `gh run view <id> --json jobs --jq '.jobs[].steps[] | select(.conclusion == "failure")'` — which step failed?
2. `gh run view <id> --log-failed | tail -80` — read the error from that step
3. Match the error against the symptoms above
4. If it's a credential / auth problem and the negative-creds scenarios all pass: check captured stdout for `Auto-discovered N credential fields from E2E_*` (item 9)
5. If it's a workflow timeout despite the workflow finishing fast: check that you're inheriting `BaseSDRIntegrationTest` (item 11)
