# public-api-and-deprecation: Importable surface and env vars
- Flag: a public (non-`_`) name, method, contract field or `application_sdk.testing` mock removed/renamed without a deprecated alias in the prior release. Markers: `@deprecated` (typing_extensions) for functions/classes; `__deprecated_members__` for enum members; `_DEPRECATED_CONSTANTS` + module `__getattr__` for constants.
- Flag: notice lacking replacement path or removal version (or one already passed); `warnings.warn` without `DeprecationWarning, stacklevel=2`; notice not inline in `warn(...)`; alias that raises instead of delegating.
- Flag: public kwarg removed/renamed/ignored without accept-and-warn; new required param; changed default; moved symbol without re-export.
- Flag: new public symbol not in `__all__` or only reachable via a `_` path; factory returning `Any`/`object`/`Union`; `**kwargs: Any`; >5 required params.
- Flag: new env var without `ATLAN_` (except `OTEL_`/`DAPR_`/`K8S_`); env var dropped/renamed with no fallback and old name not in `_REMOVED_ENV_VARS` (`application_sdk/common/env_warnings.py`); inline tunables not in `constants.py`.
- Flag: new `__init__.py` export with no test (`search_code` first).
- Don't flag: `_` names.
- Severity: critical for a public break without deprecation or an untested public API; high for kwarg/env-var breaks; medium for notices/ergonomics; low otherwise.
