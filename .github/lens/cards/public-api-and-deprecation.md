# public-api-and-deprecation: Importable surface, env vars, cross-repo values
- Flag: a public (non-`_`) name, method, kwarg or `application_sdk.testing` helper removed or renamed that was not already deprecated in the previous release (`docs/standards/symbols.md`). A red Symbol Removal Check is an obligation, not noise.
- Flag: a deprecation that doesn't delegate to the replacement, or uses the wrong marker: `@deprecated` (typing_extensions) or a `DeprecationWarning` from `__init__`/`__init_subclass__` for classes and functions; `__deprecated_members__` for enum members; `_DEPRECATED_CONSTANTS` + module `__getattr__` for constants (and no module-scope re-export of that name, which silences it).
- Flag: a deliberate break not declared as `feat!:` / `BREAKING CHANGE:`.
- Flag: a new required param, a changed default, or a moved symbol without a re-export.
- Flag: a new public symbol not in `__all__`, or only reachable via a `_` path; a new `__init__.py` export with no test.
- Flag: a new env var without `ATLAN_` (except `OTEL_`/`DAPR_`/`K8S_`); an env var dropped or renamed without a fallback and without adding the old name to `_REMOVED_ENV_VARS` (`application_sdk/common/env_warnings.py`, `docs/standards/env-vars.md`).
- Flag: a change to a value other repos read (served manifest `task_queue`, error-envelope keys, persistent-artifact prefixes, preflight payloads) without following `docs/standards/cross-repo-contracts.md`.
- Don't flag (CI enforces): contract field removal/retype (B005/B006), a notice missing its replacement or removal version (B002/B003); `_` names.
- Severity: critical for a public break without deprecation; high for kwarg/env-var/cross-repo breaks; medium for notices/ergonomics; low otherwise.
