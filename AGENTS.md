# Repository Guidelines

Atlan Application SDK is a Python library for building applications on the Atlan platform.

Package manager: `uv` (task runner via `poe` in `pyproject.toml`).

Non-standard commands used across the repo:

- `uv sync --all-extras --all-groups` (full dev/test deps)
- `uv run poe start-deps` (local Dapr + Temporal)
- `uv run poe generate-apidocs` (MkDocs + API docs)
- `uv run pre-commit run --files <file>` (run pre-commit on specific file)
- `uv run pre-commit run --all-files` (run pre-commit on all files)

**IMPORTANT**: Always run pre-commit checks before committing. CI will fail if pre-commit checks fail.

**IMPORTANT**: For Dockerfile or dependency changes, run security scans (`trivy`, `grype`) before pushing. See `docs/build-scan-overview.md`.

**Commit Rules**:
- Never add Co-Authored-By lines to commits
- Follow Conventional Commits format from `.cursor/rules/commits.mdc`

**Path Semantics (ObjectStore)**:
- Key and prefix params for ObjectStore APIs accept either `./local/tmp/...` workflow paths or object-store keys like `artifacts/...`; the SDK normalizes these internally.
- Local file params remain local paths: upload `source` and download `destination` are not treated as object-store keys.

Where to look next (progressive disclosure):

- `docs/agents/project-structure.md` — quick map of key directories.
- `docs/agents/dev-commands.md` — setup and repeatable workflows.
- `docs/agents/coding-standards.md` — formatting, logging, exceptions, performance.
- `docs/agents/testing.md` — test command, coverage config, test layout.
- `docs/agents/commits-prs.md` — commit format and PR expectations.
- `docs/agents/docs-updates.md` — which conceptual docs to update with code changes.
- `docs/agents/review.md` — code review checklist.
- `docs/agents/deepwiki.md` — DeepWiki MCP setup and when to verify SDK details.
- `docs/build-scan-overview.md` — image hierarchy, security scanning, Dapr setup.

## Security

> Follow these security guidelines for every change to the Application SDK.

### Contact

- **Security Team:** #bu-security-and-it on Slack

### Quickstart for Agents

application-sdk is the base Python framework for all Atlan App connectors. Key security-sensitive components:

- `application_sdk/clients/sql.py` — `BaseSQLClient` connects to databases using SQLAlchemy; resolves credentials from `resolved_credentials` dict; supports AWS RDS IAM authentication via `generate_aws_rds_token_with_iam_role()` (STS AssumeRole) and `generate_aws_rds_token_with_iam_user()` (IAM user token generation) in `common/aws_utils.py`.
- `application_sdk/clients/atlan.py` — wraps pyatlan `AtlanClient` with the tenant API key.
- `application_sdk/io/` — parquet and JSON I/O for object storage; paths normalized via `utils.py`.
- `application_sdk/interceptors/` — Temporal interceptors for observability.

Review every change for:

- **Credential logging in SQL client** — `BaseSQLClient` resolves credentials (username, password, connection string) from SecretStore; `resolved_credentials` dict must never appear in log output; the SQLAlchemy connection URL (which embeds the password) must never be logged; log only the database host, port, database name, and auth type (not `password` or `aws_token`).
- **AWS RDS IAM token logging** — `generate_aws_rds_token_with_iam_role()` and `generate_aws_rds_token_with_iam_user()` return RDS auth tokens that are short-lived but function as passwords; they must not be logged; the `role_arn` and `external_id` values must be validated before calling `sts.assume_role()` (reject empty/None role ARNs).
- **STS AssumeRole ExternalId** — `external_id` parameter in `generate_aws_rds_token_with_iam_role()` enforces confused-deputy protection; if an ExternalId is configured (non-None, non-empty), it must always be passed to `sts.assume_role()` — if the field is present but empty, that is a configuration error and should raise, not silently assume role without ExternalId.
- **Object storage path traversal** — `io/utils.py` normalises paths between `./local/tmp/...` workflow paths and object-store keys; callers passing `../` sequences in `key` or `prefix` parameters must not be able to read outside the expected staging prefix; validate resolved paths stay within the tenant's workflow prefix.
- **Temporal payload size** — activities that return large data (e.g., batch metadata) must use object storage references instead of inline data in return values to avoid Temporal history bloat.
- **AtlanClient API key** — the `api_key` passed to `AtlanClient` is a long-lived tenant API key; it must never appear in log output; log only the `base_url`.

### Security Invariants

- **[MUST]** `BaseSQLClient.resolved_credentials` and SQLAlchemy connection URLs must never be logged.
- **[MUST]** RDS IAM auth tokens must not be logged — treat as passwords.
- **[MUST]** STS `external_id` must be passed to `assume_role()` when configured — reject empty/None.
- **[MUST]** ObjectStore paths must be validated — no `../` traversal outside expected prefix.
- **[MUST]** AtlanClient `api_key` must never appear in log output.
- **[MUST]** All direct dependency versions in `pyproject.toml` pinned exactly.

### Data Classification

- **CONFIDENTIAL:** Database passwords, RDS IAM auth tokens, AtlanClient API key, SecretStore credentials, STS session tokens
- **INTERNAL:** Database hostname/port, role ARNs, object storage prefixes, workflow IDs, tenant IDs
- **PUBLIC:** SDK version, activity type names, connector framework version

### Review Checklist

- [ ] `BaseSQLClient.resolved_credentials` (password, auth tokens) absent from all log output
- [ ] SQLAlchemy connection URL (with embedded password) never logged
- [ ] RDS IAM auth tokens absent from all log output
- [ ] STS `external_id` passed to `assume_role()` when set; empty/None raises error
- [ ] ObjectStore key/prefix params validated — no `../` traversal outside tenant prefix
- [ ] AtlanClient `api_key` absent from all log output
- [ ] All direct dependencies in `pyproject.toml` pinned exactly
