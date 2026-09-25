# v3-architecture: Determinism, contracts, layering
- Flag: in `run()`/`@entrypoint`: I/O, `datetime.now`, `uuid.uuid4`, `random.*`, `time.time`; use `self.now()`/`self.uuid()` and put I/O in a `@task`.
- Flag: on `Input`/`Output`/`HeartbeatDetails`/contract models: a new field with no default; `= []`/`{}` not `default_factory`; v1 `class Config:`; value objects not `frozen=True`; `allow_unbounded_fields=True` without a reason; big data not in a `FileReference`.
- Flag: a new `temporalio`/`dapr` import outside `application_sdk/execution/_temporal/`/`application_sdk/infrastructure/_dapr/`, or raw `temporalio` types on the public surface (P006/P007 only warn; existing ones are backlog); any `temporalio` in app code (`@signal`/`@query`/`@update` come from `application_sdk.app`).
- Flag: `application_sdk/infrastructure/` importing `application_sdk/execution/` or `application_sdk/app/`.
- Flag: v2 APIs (`application_sdk.workflows|activities|handlers`, `*Interface`) or the deprecated SQL templates (use `SqlApp`), except in `tools/migrate_v3/`.
- Flag: `run_in_thread` wrapping the async `AtlanClient`; a workflow ID read from Temporal helpers instead of `input.workflow_id`.
- Don't flag (enforced at class definition or by CI): `@task` signature shape, `bytes`/unbounded/`Any` fields (P001), field remove/rename/retype (B005).
- Severity: critical for I/O or non-determinism in `run()`; high for new direct imports, v2 APIs, no-default fields; medium otherwise.
