---
name: add-sdr-integration-tests
description: >
  Step-by-step recipe for adding the SDR (Self-Deployed Runtime) integration
  test pipeline to a connector app. Produces a thin caller workflow on top of
  the shared `atlanhq/application-sdk/.github/actions/sdr-e2e` composite, a
  connector-specific secret-bundle generator, and a pytest suite that extends
  `BaseSDRIntegrationTest`. Covers env-var conventions, agent_json shape,
  required repo secrets, label trigger setup, and the failure modes that
  block a first run from going green (image build, app.yaml shape, env-var
  auto-discovery prefix, upload_to_atlan path shape, credential resolver
  flat-vs-nested keys).
metadata:
  author: platform
  version: "1.0.0"
  category: testing
  keywords:
    - sdr
    - self-deployed-runtime
    - integration-tests
    - github-actions
    - atlan-configurator
    - dapr
    - temporal
    - agent-credentials
---

# Add SDR Integration Tests to a Connector

You are implementing the SDR e2e test pipeline for a connector app. The shared composite action `atlanhq/application-sdk/.github/actions/sdr-e2e` does the heavy lifting (image build, configurator-generated docker compose, container orchestration, pytest run, Temporal link extraction, PR comment, commit status). The connector ships a thin caller workflow plus connector-specific bits.

## When to use this skill

- Adding SDR e2e tests to a connector that doesn't yet have them
- Migrating from a self-contained SDR harness (pre-composite) to the shared template — see `atlanhq/atlan-mssql-app#118` and `atlanhq/atlan-saperp-app#75` for reference migrations
- Diagnosing a first-run SDR failure (the **Troubleshooting** section below catches the common mistakes)

## When NOT to use this skill

- The connector's regular integration tests (`tests/integration/`) — those use a local Dapr+Temporal+app stack and a different framework path
- Writing the composite action itself (lives under `.github/actions/sdr-e2e/` in this SDK repo, not in the connector)

## Prerequisites

- Connector is on application-sdk **3.4.0+** (when `BaseSDRIntegrationTest` and the composite action shipped). Confirm with `grep atlan-application-sdk pyproject.toml`.
- Connector already has a working `Dockerfile` that produces a runnable image — the composite builds and pushes this on every label trigger.
- Connector publishes a Looker/MSSQL-style **single-credential** auth model. Multi-credential apps (e.g. SAP ECC + S4) need a tweaked `make-secrets.py` that writes multiple bundles; see the saperp PR for that pattern.

## Required deliverables

You will create three files and add four GitHub repo secrets. The skeletons are below; copy them and substitute connector-specific values.

| File | Purpose |
|---|---|
| `.github/workflows/sdr-integration-tests.yaml` | Thin caller — sets job env, invokes the composite |
| `.github/e2e/make-secrets.py` | Writes `.github/e2e/secrets/credentials.json` from env (the canonical SDR-test secrets path) |
| `tests/sdr/test_<connector>_sdr.py` | Subclasses `BaseSDRIntegrationTest`; declares `agent_spec_template` + scenarios |

## Implementation steps

### 1. Workflow caller

Create `.github/workflows/sdr-integration-tests.yaml` from this template. Replace `<APP>` with the connector short name (lower-snake, e.g. `mssql`, `looker`) and `<APP_IMAGE>` with the GHCR image name (e.g. `atlan-mssql-app`).

```yaml
name: SDR Integration Tests

on:
  pull_request:
    types: [labeled]
  workflow_dispatch:

jobs:
  sdr-tests:
    if: >-
      github.event_name == 'workflow_dispatch' ||
      github.event.label.name == 'sdr-e2e-test'
    runs-on: ubuntu-latest
    timeout-minutes: 60
    concurrency:
      group: sdr-integration-tests-${{ github.ref }}
      cancel-in-progress: true
    permissions:
      pull-requests: write
      contents: read
      statuses: write
      packages: write
    env:
      # Tenant + tenant-OAuth — the configurator and the SDK's docker-compose
      # env-var bridges read these.
      CI_TENANT_DOMAIN: ${{ secrets.SDR_TEST_TENANT }}
      CI_CLIENT_ID: ${{ secrets.SDR_CLIENT_ID }}
      CI_CLIENT_SECRET: ${{ secrets.SDR_CLIENT_SECRET }}
      # Connector creds — must be E2E_<APP_UPPER>_<FIELD>. The framework's
      # auto-discovery strips E2E_<APP_UPPER>_ based on app-name; any extra
      # prefix here lands as part of the discovered field name and breaks
      # the connector handler.
      E2E_<APP_UPPER>_HOST: ${{ secrets.<APP_UPPER>_HOST }}
      E2E_<APP_UPPER>_PORT: ${{ secrets.<APP_UPPER>_PORT || '443' }}
      E2E_<APP_UPPER>_USERNAME: ${{ secrets.<APP_UPPER>_CLIENT_ID }}
      E2E_<APP_UPPER>_PASSWORD: ${{ secrets.<APP_UPPER>_CLIENT_SECRET }}
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false

      - uses: atlanhq/application-sdk/.github/actions/sdr-e2e@main
        with:
          app-name: <app>
          app-image-name: <app-image>
          secrets-script: .github/e2e/make-secrets.py
```

Pin `actions/checkout` to a SHA per repo CLAUDE.md security policy. Track `@main` on the SDK action — the composite is treated as platform-managed.

### 2. Secret-bundle generator

Create `.github/e2e/make-secrets.py`:

```python
"""Write the SDR test secrets bundle for the <App> connector.

Reads E2E_<APP_UPPER>_USERNAME and E2E_<APP_UPPER>_PASSWORD from env
(set as job-level env vars in the calling workflow) and writes them
under the secret-store key ``<app>-credentials`` (referenced as
``secret-path`` in agent_json) to ``.github/e2e/secrets/credentials.json``.
"""

from __future__ import annotations
import json
import os

bundle = {
    "username": os.environ["E2E_<APP_UPPER>_USERNAME"],
    "password": os.environ["E2E_<APP_UPPER>_PASSWORD"],
}
out = {"<app>-credentials": json.dumps(bundle)}

os.makedirs(".github/e2e/secrets", exist_ok=True)
with open(".github/e2e/secrets/credentials.json", "w") as f:
    json.dump(out, f)
print("Wrote .github/e2e/secrets/credentials.json")
```

The bundle value **must** be a JSON-encoded string (not a nested dict) — Dapr's `local.file` secret store with `nestedSeparator=":"` expects nested-bundle-as-string on lookup. If your connector's bundle has more fields (e.g. extras for git creds), include them in the dict literal.

### 3. Test suite

Create `tests/sdr/__init__.py` (empty) and `tests/sdr/test_<connector>_sdr.py`. The base class `BaseSDRIntegrationTest` takes care of polling `GET /workflows/v1/status/{wf}/{run}` until COMPLETED for any workflow scenario, and routes credential resolution through `agent_spec_template` so workflow tests exercise the secret-store → credential-ref path (not inline creds).

```python
"""SDR integration tests for the <App> connector."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Dict

from application_sdk.testing.integration import (
    Scenario, contains, equals, is_dict, is_not_empty, is_string,
)
from application_sdk.testing.sdr import BaseSDRIntegrationTest
from dotenv import load_dotenv

_TESTS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _TESTS_DIR.parent
for path in (_REPO_ROOT / ".env", _TESTS_DIR / ".env"):
    if path.exists():
        load_dotenv(path, override=False)

_port_env = os.environ.get("E2E_<APP_UPPER>_PORT", "443")
_app_port = int(_port_env) if _port_env.isdigit() else 443
_app_host = os.environ.get("E2E_<APP_UPPER>_HOST", "")

# Username and password are *ref-keys* the SDK's CredentialResolver
# substitutes from the local.file secret store at /app/secrets/credentials.json.
_AGENT_JSON: Dict[str, Any] = {
    "agent-name": "<app>-ci-agent",
    "secret-manager": "local",
    "secret-path": "<app>-credentials",     # MUST match make-secrets.py's key
    "auth-type": "basic",
    "host": _app_host,
    "port": _app_port,
    "basic.username": "username",
    "basic.password": "password",
}

_valid_creds_base: Dict[str, Any] = {
    "username": os.environ.get("E2E_<APP_UPPER>_USERNAME", ""),
    "password": os.environ.get("E2E_<APP_UPPER>_PASSWORD", ""),
    "host": _app_host,
    "port": _app_port,
    "authType": "basic",
    "type": "all",
}

_SDR_OUTPUT_BASE = "data/artifacts/apps/<app>/workflows"


class Test<App>Sdr(BaseSDRIntegrationTest):
    timeout: int = 180
    agent_spec_template = _AGENT_JSON

    default_credentials: Dict[str, Any] = {"authType": "basic", "type": "all"}

    # Narrow the crawl to a stable, small scope so the workflow scenario
    # completes inside the 600s polling window. Tenant-wide crawls take
    # 15-30 min on shared dev tenants and race other PRs.
    default_metadata: Dict[str, Any] = {
        # connector-specific filter keys — see the existing
        # tests/integration/ suite for the right shape.
    }

    default_connection: Dict[str, Any] = {
        "connection_name": "test_<app>_sdr",
        "connection_qualified_name": "default/<app>/sdr_test",
    }

    scenarios = [
        # Auth (≥3): valid_credentials, response_structure, wrong_credentials,
        # wrong_host, wrong_username, wrong_password
        Scenario(name="auth_valid_credentials", api="auth",
                 assert_that={"success": equals(True)},
                 description="Valid creds authenticate via the SDR container"),
        # Preflight (≥3): valid_configuration, response_structure, invalid_credentials
        Scenario(name="preflight_valid_configuration", api="preflight",
                 assert_that={"success": equals(True),
                              "data.authentication.success": equals(True)},
                 description="authentication + key checks pass"),
        # Workflow (≥1): full extraction polled to COMPLETED on tenant Temporal
        Scenario(
            name="workflow_default_filters_validates_output",
            api="workflow",
            metadata={"extraction-method": "agent"},  # MUST be `agent` for SDR
            assert_that={
                "success": equals(True),
                "data.workflow_id": is_not_empty(),
                "data.run_id": is_not_empty(),
            },
            extracted_output_base_path=_SDR_OUTPUT_BASE,
            workflow_timeout=600,
            polling_interval=15,
            description="Full SDR workflow runs to COMPLETED + raw output validates",
        ),
    ]
```

For full coverage targets see `references/scenarios-template.md`.

### 4. Repo secrets

Add **four** secrets to the repo (`gh secret set <NAME> -R atlanhq/<repo>`):

| Secret | Value source |
|---|---|
| `SDR_TEST_TENANT` | Full domain of the CI test tenant (e.g. `e2e-aws.atlan.com`, `devex.atlan.com`) |
| `SDR_CLIENT_ID` | Atlan tenant **OAuth client** id (format `oauth-client-<uuid>`). NOT the connector's API credential. |
| `SDR_CLIENT_SECRET` | Atlan tenant OAuth client secret |
| `<APP_UPPER>_HOST/CLIENT_ID/CLIENT_SECRET` | Connector's API3/test credentials. Reuse from existing direct integration suite if present. |

> **Common mistake:** using the connector's API credential (e.g. a Looker API3 client_id like `w77D6DcNsd7JQcSHWr5G`) as `SDR_CLIENT_ID`. That fails with `401 Unauthorized` from the tenant's Keycloak `/auth/realms/default/protocol/openid-connect/token`. The SDR_* triple authenticates the worker container against the **Atlan tenant's IDP**, not the source system.

### 5. The `app.yaml` configurator-input file

The composite runs `envsubst < app.yaml > app-resolved.yaml`. Some connectors use the older `atlan.yaml` filename for everything else (build-and-publish-app.yaml etc.); the SDR action specifically expects `app.yaml` with the **configurator-input shape** (3 lines), not a copy of `atlan.yaml`:

```yaml
app_name: <app>
app_image: ${APP_IMAGE}
app_port: 8000
```

If `app.yaml` is missing or has the wrong shape, the run fails at `envsubst` or at `atlan-configurator` with `Error loading app configuration: app name is required`. Commit the file alongside `atlan.yaml`.

### 6. Wire the trigger

Create the `sdr-e2e-test` label if it doesn't exist (`gh label create sdr-e2e-test --description "Trigger SDR end-to-end integration tests" --color 0e8a16 -R atlanhq/<repo>`), then apply it to the PR. The workflow only fires on `labeled` events, so label-toggle (`gh pr edit <n> --remove-label sdr-e2e-test && gh pr edit <n> --add-label sdr-e2e-test`) is the canonical re-run mechanism.

## Verification checklist

When the run completes, walk this list:

- [ ] **Build & Push image** step succeeded (`ghcr.io/atlanhq/<app-image>:sdr-test-<sha>` pushed)
- [ ] **Generate secrets bundle** step produced `.github/e2e/secrets/credentials.json`
- [ ] Container reached `:8000/server/health` within `container-health-timeout-seconds`
- [ ] All auth scenarios → expected outcomes (the negative-creds scenarios are easy false-passes; verify positive ones too)
- [ ] Workflow scenario polled to **COMPLETED** (not COMPLETED→ scenario passed but no Temporal link in PR comment)
- [ ] PR comment shows clickable Temporal workflow link (`https://<tenant>/api/temporal/namespaces/default/workflows/<wf>/<run>/history`)
- [ ] GitHub Step Summary populated with the scenario table

## Troubleshooting

The first run almost always fails. The failures cluster into a small set — see `references/troubleshooting.md` for the full list with fixes. Quick reference:

| Symptom | Most likely cause |
|---|---|
| `app.yaml: No such file or directory` at `Resolve app.yaml` step | `app.yaml` missing or repo only has `atlan.yaml` (step 5) |
| `Error loading app configuration: app name is required` | `app.yaml` has the `atlan.yaml` shape (`name:` instead of `app_name:`) |
| `secrets-script not found at .github/e2e/make-secrets.py` | Path mismatch in workflow `with: secrets-script:` value |
| `CI_TENANT_DOMAIN is not set` | Forgot `env:` block in caller workflow |
| `401 Unauthorized` on `/auth/realms/default/protocol/openid-connect/token` | `SDR_CLIENT_ID/SECRET` are connector API credentials, not Atlan tenant OAuth (step 4) |
| `Required auth credentials not found.` despite valid creds | Env-var prefix mismatch — `E2E_<APP>_<FIELD>` must match `app-name` exactly, no extra suffix (`_BASIC_` etc.) |
| `[AAF-STR-004] local_path does not exist` from `upload_to_atlan` | Connector passing object-store key (from `get_object_store_prefix()`) instead of the real local fs `output_path` to `UploadInput.output_path`. Fix on the connector side; pass `output_path` directly. |
| Workflow scenario passes assertions but PR comment is missing Temporal link | Tests didn't poll completion — set `workflow_timeout` > 0 and inherit from `BaseSDRIntegrationTest` (not plain `BaseIntegrationTest`) |

## References

- `references/scenarios-template.md` — recommended scenario coverage per API surface
- `references/troubleshooting.md` — full failure-mode catalogue with diagnosis steps
- `references/connector-conventions.md` — agent_json shape, secret-store path conventions, multi-credential connectors

## Companion: existing implementations

- [`atlan-mssql-app/tests/sdr/test_mssql_sdr.py`](https://github.com/atlanhq/atlan-mssql-app/blob/main/tests/sdr/test_mssql_sdr.py) — single-credential SQL connector
- [`atlan-saperp-app/tests/sdr/test_saperp_sdr.py`](https://github.com/atlanhq/atlan-saperp-app/blob/main/tests/sdr/test_saperp_sdr.py) — multi-credential pattern (ECC + S4)
- [`atlan-looker-app/tests/sdr/test_looker_sdr.py`](https://github.com/atlanhq/atlan-looker-app/blob/main/tests/sdr/test_looker_sdr.py) — REST connector with FLL
