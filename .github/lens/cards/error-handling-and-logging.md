# error-handling-and-logging: Exceptions and logs
- Flag: `except` that logs and continues on DB/network/file/handler/storage/app paths; re-raise or wrap. Swallowing is OK only in `application_sdk/observability/`, metric/trace/event emits, signal handlers and cleanup.
- Flag: an error that is not an `application_sdk.errors` leaf: `AuthError`, `AppPermissionDeniedError`, `DependencyUnavailableError` (Atlan-internal service), `SourceUnavailableError` (customer-owned source), `InvalidInputError`, `NotFoundError`, `AlreadyExistsError`, `PreconditionError`, `RateLimitedError`, `AppTimeoutError`, `ResourceExhaustedError`, `DataIntegrityError`, `CancelledError`, `UnimplementedError`, `InternalError`. Prefer a domain subclass that sets `code` over raising the leaf itself.
- Flag: the wrong leaf for the failure (e.g. `DependencyUnavailableError` for a customer database that is down).
- Flag: caught exception text copied into `message=` or a contract message field; evidence kwargs holding secrets.
- Flag: `logger.exception(...)` (use `logger.error(..., exc_info=True)`, ADR `docs/adr/0011-logging-level-guidelines.md`); `logger.error` for a recovered fallback (WARNING).
- Flag: user-facing messages that don't say what failed and what to do.
- Flag: credentials, tokens, auth headers or DSNs in any log call.
- Don't flag (CI enforces): stdlib `logging.getLogger`, missing `exc_info`/`from e`, bare `Exception`/`RuntimeError`, `AAF-*` codes, `logger.critical`, f-string/%-format logs, `print`, except-pass.
- Severity: critical for a secret in a log; high for a swallowed failure in a task/handler/storage path or a wrong leaf (misroutes retries and alerts); medium otherwise.
