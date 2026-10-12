# Hosted handler code (P056)

An app that declares `[tool.atlan-app-api]` in its root `pyproject.toml` has its
handler served by the consolidated API host. The host installs the handler's own
`app/` files on `atlan-application-sdk-api`, which carries no structured logger.
Handler code reports through its return value (`AuthOutput`, `PreflightOutput`
and its checks, `MetadataOutput`) or a typed `AppError`. The SDK's shared routes
log every outcome with the request id, on the worker and on the host alike.

## Fix

1. From the app repo root, run application-sdk's fixer:

   ```bash
   python3 <application-sdk>/.github/scripts/gen_app_api.py fix
   ```

   It deletes the logging statements in the handler's files (a block left empty
   gets `pass`) and refuses to finish if a removed logger is still referenced.

2. Review every removal. Where a log line was the only report of a failure,
   raise a typed `AppError` (or return a failed check) instead, so the failure
   reaches the caller on the wire.

3. Remove comments and variables that only existed for the removed log lines,
   then run the app's tests.

A line that genuinely must stay records why with
`# conformance: ignore[P056] <reason>`.
