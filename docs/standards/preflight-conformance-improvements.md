# Preflight conformance improvements and validation

Validated 2026-09-08 against the same 16 clean local connector snapshots used in the workbook audit. This is static package validation, not live source or Temporal verification. The original findings remain in preflight-category-validation.md as the before-state.

## Changes

- P034 recognizes public SDK re-exports and the default false verdict. Invalid status= on a check is separately diagnosed by P052.
- Dynamic failed-check conditions, unresolved error construction, and computed aggregation emit P065. SARIF exposes atlan/analysisStatus=unresolved. These are investigation requirements, not confirmed source-policy violations.
- P053 follows errors into failed-check outputs through helpers and arguments. Replaced intermediate errors, raised-only branches, unreachable factory returns, and resolved error=None success paths no longer produce guidance false positives.
- Every preflight rule links to a packaged guide covering its contract, evidence, remediation, false positives, tests, and analysis limits.
- Selecting only preflight rules avoids unrelated P-series checkers. A regression test covers this path.

## Category results

| Category | Samples | Final result |
| --- | --- | --- |
| Fully untyped | BigQuery, S3, Google Cloud Lineage | S3/GCL directly detected; BigQuery dynamic wrapper is explicitly unresolved. |
| Structure | Hive, Mode, Monte Carlo | Computed aggregation is explicitly unresolved. Mandatory/advisory and transient policy require scenarios. |
| Partially untyped | Looker, Fivetran, Snowflake | Looker/Snowflake typing defects retained; Fivetran dynamic verdict is explicitly unresolved. Snowflake registry-driven guidance remains incomplete. |
| Missing guidance | dbt, Salesforce, Trino | Valid P053 findings retained for all three. |
| Clean typing/guidance | Hightouch, Cognos, Athena | Hightouch clean. Cognos/Athena false-positive P053 findings removed; separate migration, consistency and uncertainty findings remain. |
| No handler | UKG | No findings, unchanged. |

Seven of 12 problem-category samples now have direct detection of the primary defect; the other five are explicitly unresolved. That is not 12 verified implementations. Source-specific scenarios are still required for behavioral conformance.

## Validation

- Built the wheel and loaded the package from that wheel while running the CLI against all 16 apps. Verified packaged Python/Markdown files match the current source and all 19 guide sections are present.
- All 16 CLI invocations completed. Exit0 reflects warning-tier findings, not certification. No app files were changed.
- Full conformance suite: 3,543 passed, one expected failure, two pre-existing SDK contract-registry failures involving workflow_slug. Both failures previously reproduced with the unchanged baseline test file.
- Pre-commit checks and generated rule-document freshness checks passed. New regressions cover confirmed misses, false positives, uncertainty, fixed-result disappearance, and rule-selection isolation.
- No new inline comments or customer-identifying examples were added. Connector names are retained.

Wheel SHA-256: `6e0b998053a263c977b4052ecf8b24e6c1a64437df23195c80a223bcfe5919e6`.

## Exact snapshots and findings

| App | Revision | Rule counts |
| --- | --- | --- |
| bigquery | `959d398356458b3bdf16e79fc1c3f2d974b2f5eb` | P033: 2, P047: 2, P060: 1, P065: 2 |
| s3 | `004a6c2e5deeaeb4f3baf05cb10ffe60d557a28e` | P034: 1, P057: 2, P060: 1 |
| google-cloud-lineage | `aaab134050747849ce25b0f3467772c967205e14` | P033: 1, P034: 1, P052: 1 |
| hive | `367c5e504b1eb9c0045f1187aac9f31b4135a88d` | P047: 8, P053: 1, P054: 1, P060: 3, P065: 13 |
| monte-carlo | `b5452f1c2b385d0e44d8caef6916661f51011106` | P047: 2, P065: 3 |
| mode | `1837573e4f857fde7d9bad1c8e9582a56d823595` | P053: 4, P065: 4 |
| looker | `ba70137ac86b8381c96b57ec286cabc8d2b48ce2` | P034: 2, P047: 1, P053: 5, P057: 2, P060: 4, P065: 5 |
| fivetran | `7f5e2f969f4f9cf8c24bdad3da3a94795be310ab` | P047: 10, P054: 3, P065: 2 |
| snowflake | `0512c85d23367080199a2c59b774f492a0ed37e0` | P033: 2, P034: 2, P047: 2, P052: 1, P053: 1, P054: 1, P060: 3, P065: 3 |
| dbt | `7d3de42f88b7d9426b3c4ed8503be3a46ccff1e5` | P047: 8, P053: 22, P054: 6, P060: 1, P065: 3 |
| salesforce | `363356779cfdb5a88405e29f04d5af96b97de93f` | P053: 2, P065: 4 |
| trino | `81768eae4deb770957eb5ad6cd735baec8195e5f` | P047: 1, P053: 1, P054: 3, P065: 2 |
| hightouch | `e839b0fccdd968895530491126544e82453bf9d9` | None |
| cognos | `0c821d4755789660d9ec1261f4de8319b351552d` | P054: 1, P065: 2 |
| athena | `d481304fa6ff98258236499ef9044dbacbf7c15e` | P047: 6, P055: 1, P065: 4 |
| ukg | `aad0dd5b670ea9dc48213971078a485f9c068b8a` | None |
