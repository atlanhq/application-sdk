# build-config: Dockerfile, pyproject, pre-commit
- Flag: credentials in `ARG`/`build-args` (kept in image history); use `RUN --mount=type=secret`.
- Flag: `FROM ...:latest` or unpinned bases; `curl ... | sh`; runtime images running as root without reason; `COPY` of `.env` or credential files.
- Flag: new direct dependency without version constraints, a major bump, a new index, `--trusted-host`/`--allow-insecure`, or a lowered/removed release-age cooldown (`exclude-newer`, `min-release-age`).
- Flag: weakened gates: lower `fail_under`, disabled ruff/pyright rules or pre-commit hooks, broader excludes (a pyright `exclude` list replaces the defaults, so it must keep `.venv`/`node_modules`).
- Flag: tool versions (Python, `pkl`) restated instead of read from their single pin (Dockerfile base tag, `application_sdk/pkl_version.py`).
- Flag: extras/entry-point/package-data changes that drop a module or file consumers import.
- Don't flag: CVEs Trivy already blocks; lockfile contents.
- Severity: critical for a published credential or a disabled security check; high for supply-chain risk or a weakened CI gate; medium otherwise.
