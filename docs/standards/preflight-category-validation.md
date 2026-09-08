# Workbook category validation of preflight conformance

Date: 2026-09-08. Result: the checker is not yet reliable across the verified audit categories.

Read-only comparison against `Typing + suggested_action (verified 2026-09-07).xlsx`, tracker and verification sheets. Used corrected categories, not the old-row verification verdicts. Sampled three rows per priority group using Python random.Random(812), then included the sole NO HANDLER row explicitly: 16 apps total.

Workbook SHA-256: `b53aabfd0598bac12eb9c01c89834873478c6c1ae7daf7d643e13cb819dd0e4c`.

Ran the existing editable conformance installation through its CLI, not only scan_all, with --scope app --static and P032–P035, P047, P052–P062, P065 selected. All 16 invocations completed with exit0. All relevant rules warn, so exit0 is not a clean bill of health. P062 was not evaluated. No app tests, network probes, Temporal workflows, wheel installation, or latest-remote synchronization were performed. All 16 source checkouts were clean, and revisions are recorded below.

The category assessment counts detection of the primary workbook defect, not whether an app produced any warning. Five of12 problem-category apps had the primary defect identified; six were missed. Monte Carlo was not detected and its prescribed fail-open outcome remains policy-dependent. Of three CLEAN controls, Hightouch stayed clean; Cognos and Athena produced false-positive guidance warnings. UKG correctly produced no findings.

| Category | App | Workbook tracker row | Assessment | Source evidence |
| --- | --- | ---: | --- | --- |
| P0 - fully untyped | bigquery | 2 | Miss | P034 absent. Variable passed and missing error at app/handler/handler.py:1223. P053 elsewhere does not detect loss of typed errors in the result wrapper. |
| P0 - fully untyped | s3 | 5 | Miss | P034 absent despite literal passed=False without error at app/handler.py:93. Public application_sdk.handler re-export is not recognized by P034. |
| P0 - fully untyped | google-cloud-lineage | 4 | Miss | No typing finding at app/handlers/handler.py:103, where invalid status= replaces passed= and error is absent. P033/P065 concern the separate workflow task. |
| P1 - structure | hive | 9 | Miss; secondary hit | No P055 for schema_access asymmetry: connection-limit branch app/handlers/hive_handler.py:438 leads to PARTIAL at567, whereas access denial returns NOT_READY at466. P053 correctly catches the separate guidance gap at527. |
| P1 - structure | monte-carlo | 11 | Not detected; policy unresolved | Only P047. Catch-all app/handlers/handler.py:135–156 collapses non-auth failures into blocking SourceUnavailableError. The workbook blanket fail-open recommendation needs qualification: app/clients/graphql_client.py retries connection failures before surfacing exhaustion. Recovery and persistent failure need separate scenarios. |
| P1 - structure | mode | 10 | Miss; secondary hit | No P055 for all(c.passed) aggregation at app/handlers/handler.py:122–124 making the datasource advisory failure from app/preflight.py:180–187 blocking. P053 correctly catches separate missing guidance at app/failures.py:196. |
| P2 - partially untyped | looker | 16 | Hit | P034 correctly flags both untyped checks at app/handlers/looker.py:434 and454. P053 also identifies missing guidance. |
| P2 - partially untyped | fivetran | 15 | Miss | No P034 at app/handler.py:797: passed=not missing_optional has no typed error. Other warnings do not identify the workbook typing defect. |
| P2 - partially untyped | snowflake | 21 | Hit for category; incomplete guidance | P034 correctly flags app/handler/handler.py:538 and582. P053 misses guidance in dynamically dispatched probes, including _check_au_schemas at684–689. |
| P3 - missing guidance | dbt | 30 | Hit | P053 identifies missing guidance in app/handlers/__init__.py, including797,818,833,845,851 and854. Shared failed-check construction at119 carries those errors. |
| P3 - missing guidance | salesforce | 45 | Hit | P053 correctly identifies InternalError factory app/handlers/salesforce.py:143, used at241 and353. The additional timeout-error finding at136 also lacks guidance. |
| P3 - missing guidance | trino | 51 | Hit | P053 correctly identifies InternalError factory app/handlers/trino.py:256, used at474,528,628,675 and779. One factory finding can cover several workbook call sites. |
| CLEAN | hightouch | 65 | Clean classification preserved | No findings. Single-check handler app/handlers/__init__.py:51–90 supplies typed AuthError and suggested_action; no structural short-circuit requirement applies. |
| CLEAN | cognos | 58 | False-positive guidance | P053 at app/handlers/cognos.py:134 flags an InternalError that the caller raises at325 rather than attaching to a failed check. P053 also flags the raised transient at84. Separate P054 migration concerns do not make these failed-check guidance findings correct. |
| CLEAN | athena | 54 | False-positive guidance | P053 flags intermediate classifier results at app/failures.py:739–764, although app/handlers/athena_handler.py:176–207 reconstructs final errors with suggested_action. P053 at520 concerns passed=True with an error; that is a separate consistency issue, not an untyped failed check. |
| NO HANDLER | ukg | 75 | Not applicable preserved | No findings. Workbook NO HANDLER row describes a scaffold with no gated preflight implementation. No runtime conformance is implied. |

## Detector gaps demonstrated

1. P034 omits supported public re-exports. A synthetic failed check imported from application_sdk.handler produced no finding; the identical code imported from application_sdk.handler.contracts produced P034. See packages/conformance/conformance/suite/checks/preflight/_common.py:231.
2. P034 only resolves a bounded literal False case. BigQuery variable verdicts and Fivetran conditional failure remain unreported. See _untyped_failure.py:87.
3. Missing passed and invalid status keywords are unchecked. Constructing the Google Cloud Lineage pattern against the installed SDK reproduced passed=False, with no status field in the model.
4. P055 requires a literal nonempty check list and literal status, so it cannot evaluate the sampled aggregation/short-circuit policies. See _contracts.py:242. These need meaningful scenario adapters or stronger analysis; registering a TEST rule alone does not execute that evidence.
5. P053 analyzes all reachable typed constructors, rather than only errors that reach failed-check outputs. It therefore flags replaced intermediate errors and raised-only branches. See _contracts.py:379.
6. Registry-dispatched probes are not fully traversed, leaving Snowflake guidance gaps. P065 emitted elsewhere does not identify or explain this uncovered dispatch.

## Interpretation limits

The workbook is evidence to verify, not an infallible expected-output file. It predates the target strict-gate migration semantics: its raised-transient fail-open recommendations need separate evaluation against PR3685. Persistent source failure and a recoverable transient are different cases. Clean typing/guidance does not certify an app against every new preflight rule.

Finding counts and workbook site counts need not match. Salesforce and Trino share error factories across several callers. A single correctly located factory finding can cover several reported sites.

No checker fixes were applied during this comparison. The results describe the current implementation and identify the regressions needed before claiming category-wide coverage.

## Snapshot revisions and emitted rules

| App | Revision | Rule counts |
| --- | --- | --- |
| bigquery | `959d398356458b3bdf16e79fc1c3f2d974b2f5eb` | P033: 2, P047: 2, P053: 12, P060: 1, P065: 1 |
| s3 | `004a6c2e5deeaeb4f3baf05cb10ffe60d557a28e` | P057: 2, P060: 1 |
| google-cloud-lineage | `aaab134050747849ce25b0f3467772c967205e14` | P033: 1, P065: 1 |
| hive | `367c5e504b1eb9c0045f1187aac9f31b4135a88d` | P047: 8, P053: 5, P054: 1, P060: 3 |
| monte-carlo | `b5452f1c2b385d0e44d8caef6916661f51011106` | P047: 2 |
| mode | `1837573e4f857fde7d9bad1c8e9582a56d823595` | P053: 5, P065: 1 |
| looker | `ba70137ac86b8381c96b57ec286cabc8d2b48ce2` | P034: 2, P047: 1, P053: 16, P057: 2, P060: 4 |
| fivetran | `7f5e2f969f4f9cf8c24bdad3da3a94795be310ab` | P047: 10, P053: 3, P054: 3 |
| snowflake | `0512c85d23367080199a2c59b774f492a0ed37e0` | P033: 2, P034: 2, P047: 2, P052: 1, P053: 2, P054: 1, P060: 3, P065: 2 |
| dbt | `7d3de42f88b7d9426b3c4ed8503be3a46ccff1e5` | P047: 8, P053: 28, P054: 6, P060: 1 |
| salesforce | `363356779cfdb5a88405e29f04d5af96b97de93f` | P053: 2 |
| trino | `81768eae4deb770957eb5ad6cd735baec8195e5f` | P047: 1, P053: 7, P054: 3 |
| hightouch | `e839b0fccdd968895530491126544e82453bf9d9` | None |
| cognos | `0c821d4755789660d9ec1261f4de8319b351552d` | P053: 2, P054: 1 |
| athena | `d481304fa6ff98258236499ef9044dbacbf7c15e` | P047: 6, P053: 10 |
| ukg | `aad0dd5b670ea9dc48213971078a485f9c068b8a` | None |
