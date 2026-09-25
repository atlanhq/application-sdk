# error-handling-and-logging: Exceptions and logs
- Flag: `except` that logs and continues on DB/network/file/handler/storage/app paths; re-raise or wrap. Swallowing is OK only in `observability/`, metric/trace/event emits, signal handlers and cleanup, and needs `exc_info=True`.
- Flag: `raise X(...)` inside `except ... as e:` without `from e`; uncommented `from None`.
- Flag: caught exception logged without `exc_info=True`/`logger.exception`; `traceback.format_exc()` in a message.
- Flag: new bare `AppError`, `Exception`/`RuntimeError`, or legacy `error_code=`/`AAF-*`. Use the `application_sdk.errors` leaf: `AuthError`, `AppPermissionDeniedError`, `DependencyUnavailableError`, `InvalidInputError`, `NotFoundError`, `AlreadyExistsError`, `PreconditionError`, `RateLimitedError`, `AppTimeoutError`, `ResourceExhaustedError`, `DataIntegrityError`, `InternalError`.
- Flag: user messages lacking what/why/how-to-fix.
- Flag: `logger.critical()`; `logger.error` for recoverable cases; stdlib `logging.getLogger` in app code (use `get_logger(__name__)`).
- Flag: credentials, tokens, auth headers or DSNs in any log call.
- Don't flag: f-string/%-format logs, `print`, `logger.warn`, except-pass (CI owns them).
- Severity: critical for a secret in a log; high for a swallowed failure in a task/handler/storage path; medium for missing `from e`/`exc_info` or wrong leaf; low otherwise.
