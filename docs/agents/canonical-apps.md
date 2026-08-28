# Canonical apps

Four public repos are the reference implementations for apps built on this SDK. When you need to know how something *should* look in a consumer app — test layout, contract shape, entrypoint wiring, credential handling — read one of these rather than generalising from an arbitrary `atlan-*-app`.

| Repo | Why it's the reference |
|---|---|
| [`atlan-hello-world-app`](https://github.com/atlanhq/atlan-hello-world-app) | Smallest complete app. The shape a scaffold produces, with nothing connector-specific in the way. |
| [`atlan-openapi-app`](https://github.com/atlanhq/atlan-openapi-app) | Non-SQL, public source — no credential config (`hasCredentialConfig = false`). Shows the shape when there is nothing to authenticate against. |
| [`atlan-mysql-app`](https://github.com/atlanhq/atlan-mysql-app) | SQL connector, real credentials. The fullest test suite: handler auth/preflight against a real MySQL including negative cases, credential resolution against fake secret stores, and a full-DAG e2e. |
| [`atlan-metabase-app`](https://github.com/atlanhq/atlan-metabase-app) | BI connector, real credentials, multi-entrypoint. Shows the non-SQL path on `BaseE2ETest`. |

## Why this list matters

Most `atlan-*-app` repos are not safe to copy from. At any time some are mid-migration, some carry patterns the SDK has since deprecated, and some solved a problem locally that the SDK now solves centrally. A survey across arbitrary connector repos will therefore reproduce whatever the fleet's median staleness is — it cannot tell you what is correct.

Two authorities settle "what is correct": **this repo's own tests**, and **the four apps above**. Nothing else is evidence.

## What they establish about test layout

`atlan-openapi-app`, `atlan-mysql-app` and `atlan-metabase-app` each have exactly three test directories — `unit/`, `integration/`, `e2e/`. `atlan-hello-world-app` is the scaffold shape and currently has `tests/unit/` only; treat that as what a new app starts with, not as a gap to fill in the other three.

None of the four has a `tests/sdr/` or a `tests/full_dag/`, and none uses `Scenario` or `BaseIntegrationTest`. Concerns are placed like this:

| Concern | Where | Example |
|---|---|---|
| Handler functionality — auth, preflight | called directly, negative cases included | mysql `tests/integration/test_mysql_handler.py` (real MySQL); metabase `tests/unit/test_handler.py` (mocked client) |
| Credential resolution | proven once in this repo (`tests/unit/credentials/`); per-app, against fake secret stores | mysql `tests/integration/test_credential_resolution.py` |
| A full DAG, in either run mode | `tests/e2e/` via the generated `app/generated/_e2e_base.py` | mysql `tests/e2e/test_mysql_e2e.py` |

Tier 4 vs tier 5 is the `mode` ClassVar (`RunMode.AGENT` / `RunMode.DIRECT`) on one base class — not a different base class and not a separate test directory. See `docs/standards/connector-ci-e2e.md`.
