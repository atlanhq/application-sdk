# SDR Scenario Coverage Template

Recommended scenario set per API surface. The base class
`BaseSDRIntegrationTest` runs each scenario as a separate pytest
test; failures appear individually in the GitHub Step Summary and
PR comment.

## Auth (≥ 5 scenarios)

| Name | Override | Assertion | Why |
|---|---|---|---|
| `auth_valid_credentials` | none | `success: True` | Baseline — valid creds authenticate |
| `auth_response_structure` | none | `data: dict`, `message: string` | Locks the response shape against drift |
| `auth_wrong_username` | `username: "definitely_not_real"` | `success: False` | Distinguishes username-side rejection from secret-side |
| `auth_wrong_password` | `password: "definitely_wrong"` | `success: False` | Mirror — most APIs return identical 401 either way; both tests document intent |
| `auth_wrong_host` | `host: "https://this-host-does-not-resolve.invalid"` | `success: False` | Network-layer failure (DNS / connect refused) maps to handler 401 |

> **False-pass trap:** "wrong" scenarios passing tells you nothing — the handler returns `success: False` when creds are missing entirely too. Always include `auth_valid_credentials` (and verify it passes) before trusting the negative scenarios.

## Preflight (≥ 5 scenarios)

The connector's preflight handler emits one `data.<check>.success` boolean per check. Match the scenarios to the actual checks the handler emits — don't assume.

```python
# Discover what your handler emits:
grep -n "PreflightCheck(name=" app/handlers/<connector>.py
```

| Name | Override | Assertion |
|---|---|---|
| `preflight_valid_configuration` | none | `success: True`, `data.<each-check>.success: True` |
| `preflight_response_structure` | none | `data.<each-check>: dict`, `data.<each-check>.message: string` |
| `preflight_<check_name>` | none | `data.<check_name>.success: True` (one per significant check) |
| `preflight_invalid_credentials` | wrong creds | `success: False` |
| `preflight_<connector-specific>` | e.g. narrow include filter | check-specific assertion |

Examples by connector:

- **MSSQL**: `instance_reachable`, `database_listing`, `schema_listing`, `table_listing`
- **Looker**: `authentication`, `project_access`, `git_credentials` (FLL only)
- **SAP**: `auth`, `bw_setup` (ECC vs S4 specific checks)

## Workflow (≥ 1 scenario)

The full-workflow scenario is the value-prop of SDR e2e — it exercises:

1. The agent_json → secret-store → ref-key resolution path (validates SDR credential resolution end-to-end)
2. Container → tenant Temporal auth (validates the `SDR_CLIENT_*` triple)
3. Workflow execution end-to-end (validates the connector's actual extract logic)
4. Output validation against the mounted `./data` volume (validates artifacts land where downstream consumers expect)

```python
Scenario(
    name="workflow_default_filters_validates_output",
    api="workflow",
    metadata={
        # MUST set extraction-method=agent — that's what tells the SDK
        # to route credential resolution through agent_json → secret-store
        # rather than reading inline credentials from the request.
        "extraction-method": "agent",
        # Connector-specific filters that NARROW the crawl to a stable,
        # small dataset — tenant-wide crawls take 15-30 min on shared dev
        # tenants and exceed the 600s polling window. Pick a folder/schema
        # that exists, is small, and won't drift.
        "include-projects": '{"<stable-narrow-scope>": {}}',
    },
    assert_that={
        "success": equals(True),
        "data.workflow_id": is_not_empty(),
        "data.run_id": is_not_empty(),
    },
    extracted_output_base_path=_SDR_OUTPUT_BASE,
    workflow_timeout=600,    # poll up to 10 min for COMPLETED
    polling_interval=15,
    description="Full SDR workflow runs to COMPLETED + raw output validates",
),
```

> **`extraction_method=agent` matters.** Without it, `CredentialRef.from_workflow_args` falls through to the legacy GUID route, which uses inline creds and skips the secret-store path the SDR setup is testing. Symptom: tests pass against the SDR container but exercise the same code path as `tests/integration/`.

> **`workflow_timeout=0` is a footgun.** With it set to zero (or omitted), `BaseSDRIntegrationTest._execute_scenario` short-circuits and treats the scenario as "passed" the moment `start_workflow` returns success — even if the workflow then fails on the tenant's Temporal cluster. Always set `workflow_timeout > 0` for full-completion scenarios.

## Optional add-ons

| Scenario | Use when |
|---|---|
| `workflow_invalid_filter` | Connector's extract phase has filter validation that should fail-fast |
| `workflow_extracted_record_counts` | Output schema is stable and you want regression-testing on extracted record counts |
| `workflow_baseline_diff` | You have a curated golden output for regression — set `expected_data` on the scenario |

## Anti-patterns

- **Don't add scenarios for code paths SDR mode doesn't exercise.** SDR mode is `extraction_method=agent`. If your scenario uses `extraction_method=direct`, it belongs in `tests/integration/`, not `tests/sdr/`.
- **Don't duplicate the direct integration suite.** SDR tests are slower (~3-5 min vs <30s), cost real tenant Temporal capacity, and pin to a specific test tenant. Keep the suite focused on what SDR exercises that direct mode doesn't.
- **Don't poll Temporal directly.** `BaseSDRIntegrationTest._ensure_workflow_completed` polls the connector's HTTP `/workflows/v1/status/{wf}/{run}` endpoint, which proxies to Temporal. Direct Temporal client connections from the runner add credential complexity for no gain.
