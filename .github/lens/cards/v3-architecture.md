# v3-architecture: Determinism, contracts, layering
- Flag: in `run()`/`@entrypoint`: any I/O, `datetime.now`, `uuid.uuid4`, `random.*`, `time.time`. Use `self.now()`/`self.uuid()`; put I/O in a `@task`.
- Flag: on `Input`/`Output`/`HeartbeatDetails`/`application_sdk/contracts/` models: a new field with no default; `= []`/`{}` not `Field(default_factory=...)`; `@dataclass` on a BaseModel; v1 `class Config:`; value objects not `frozen=True`; `allow_unbounded_fields=True` without a reason.
- Flag: big data in a contract field instead of a `FileReference`.
- Flag: a NEW `temporalio`/`dapr` import outside `application_sdk/execution/_temporal/` and `application_sdk/infrastructure/_dapr/` (existing ones are known backlog, P006); any `temporalio` import in app code — `@signal`/`@query`/`@update` come from `application_sdk.app`.
- Flag: `application_sdk/infrastructure/` importing `application_sdk/execution/` or `application_sdk/app/`.
- Flag: v2 APIs (`application_sdk.workflows|activities|handlers`, `*Interface` subclasses) or the deprecated templates (`SqlMetadataExtractor`, `IncrementalSqlMetadataExtractor`, `SqlQueryExtractor`, `BaseMetadataExtractor`; use `SqlApp`).
- Flag: `run_in_thread` wrapping the async `AtlanClient`; a workflow ID read from Temporal helpers instead of `input.workflow_id`.
- Don't flag (enforced at class definition or by CI): `@task` signature shape, `bytes`/unbounded/`Any` contract fields, field remove/rename/retype (B005), non-determinism in `run()` (P020/P021).
- Severity: critical for I/O or non-determinism in `run()`; high for new direct imports, v2 APIs, no-default fields; medium otherwise.
