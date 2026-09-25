# observability: Metrics, log attributes, outcome events
- Flag: metric label values from `str(e)`/`repr(e)`, run/workflow/trace IDs, paths, table names or query text; use `error_type=type(e).__name__` (`docs/standards/metrics.md`).
- Flag: one metric name emitted with different label-key sets at two sites (Pushgateway rejects the whole push); `job`/`instance` passed as labels.
- Flag: a structured log kwarg meant to be queryable with no `_KNOWN_EXTRA_KEYS` entry or passthrough prefix (`application_sdk/observability/logger_adaptor.py`): it is dropped from OTLP.
- Flag: a new outcome event not added to `application_sdk/observability/events.py`; any rewording of an existing event name or lifecycle token (dashboards and alerts key on them).
- Flag: diagnostics that must survive run-log export put only in attributes; the export keeps the message body (`docs/concepts/monitoring.md`).
- Severity: high for a push-breaking label schema, a cardinality bomb, or a renamed event; medium for a dropped attribute; low otherwise.
