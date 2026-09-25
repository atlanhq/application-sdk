# error-handling-and-logging: Exceptions and logs
- Flag: `except` that logs and continues on DB/network/file/handler/storage paths; re-raise or wrap (swallowing is OK only in observability emits, signal handlers, cleanup).
- Flag: an error that isn't the most specific `application_sdk.errors` leaf (`application_sdk/errors/leaves.py`), e.g. `DependencyUnavailableError` (Atlan service) vs `SourceUnavailableError` (customer source); prefer a domain subclass setting `code`.
- Flag: `retryable` flipped on an existing leaf or failure path (it is Temporal's retry decision).
- Flag: `exc_info=True` or a formatted exception at a connect/auth/token site: the traceback bypasses redaction; use `sanitize_cause_repr`/`redact_secrets` (critical).
- Flag: caught exception text copied into `message=` or a contract message (E015/E019); messages that don't say what failed and what to do.
- Flag: `logger.exception(...)` (use `logger.error(..., exc_info=True)`, ADR-0011); `logger.error` for a recovered fallback (WARNING); credentials, tokens or DSNs in any log.
- Flag (CI only warns): missing `from e` (E016), bare `ValueError`/`RuntimeError` (E012), `logger.critical` (L007), an except that returns/assigns without logging (E007).
- Don't flag (CI blocks): stdlib `logging.getLogger` (L002), missing `exc_info` on a logged exception (L004), `AAF-*` raises (E013), f-string logs, `print`, except-pass.
- Severity: critical for a secret in a log; high for a swallowed failure or wrong leaf in a task/handler/storage path; medium otherwise.
