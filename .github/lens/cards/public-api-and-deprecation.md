# public-api-and-deprecation: Importable surface, env vars, cross-repo values
- Flag: a public name, method, kwarg or `application_sdk.testing` helper removed/renamed without being deprecated in the previous release (`docs/standards/symbols.md`); a red Symbol Removal Check is an obligation.
- Flag: a deprecation that doesn't delegate, or the wrong marker: `@deprecated` or an `__init__` `DeprecationWarning` (classes/functions), `__deprecated_members__` (enum members), `_DEPRECATED_CONSTANTS` + `__getattr__` (constants; no module-scope re-export).
- Flag: a deliberate break not declared `feat!:`/`BREAKING CHANGE:`; a new required param, changed default, or moved symbol without re-export.
- Flag: a new public symbol not in `__all__`; a new `__init__.py` export with no test.
- Flag: a new env var without `ATLAN_` (except `OTEL_`/`DAPR_`/`K8S_`); one removed/renamed without a fallback and `_REMOVED_ENV_VARS` entry (`docs/standards/env-vars.md`).
- Flag: a value other repos read (served `task_queue`, error-envelope keys, persistent prefixes, preflight payloads) changed without `docs/standards/cross-repo-contracts.md`.
- Flag (CI only warns): a notice without replacement and removal version (B002), or past its removal version (B003).
- Don't flag (CI blocks): contract field removal/retype (B005/B006); `_` names.
- Severity: critical for a public break without deprecation; high for kwarg/env-var/cross-repo breaks; medium otherwise.
