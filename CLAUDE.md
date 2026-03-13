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

This is the base Python SDK framework for all Atlan App connectors. It provides: SQL client with AWS RDS IAM auth (`clients/sql.py`), SecretStore (Vault) integration, object storage (S3/GCS/ADLS), Temporal workflow scaffolding, and structured logging. Review every change for:

- **Credential logging** — `BaseSQLClient` holds resolved credentials; resolved passwords, tokens, and connection strings must never be logged; log only hostname, port, database, and auth type.
- **AWS RDS IAM token leakage** — `generate_aws_rds_token_with_iam_role()` and `generate_aws_rds_token_with_iam_user()` return short-lived RDS auth tokens; these tokens must not be logged; treat them as passwords.
- **STS ExternalId enforcement** — `generate_aws_rds_token_with_iam_role()` accepts `external_id`; if an ExternalId is configured, it must always be passed to `sts.assume_role()` — never silently dropped.
- **ObjectStore path scoping** — SDK normalises `./local/tmp/...` paths and object-store keys internally; callers must not be able to escape the expected prefix via `../` traversal in key/prefix parameters.

### Security Invariants

- **[MUST]** Resolved credentials (passwords, tokens, connection strings) must never be logged.
- **[MUST]** RDS IAM auth tokens must not be logged — treat as passwords.
- **[MUST]** STS ExternalId must be passed when configured — never silently dropped.

### Review Checklist

- [ ] `BaseSQLClient.resolved_credentials` never logged
- [ ] RDS IAM auth tokens absent from all log output
- [ ] STS `external_id` passed to `assume_role()` when set
- [ ] ObjectStore key/prefix params validated — no `../` traversal
- [ ] All direct dependencies in `pyproject.toml` pinned exactly
