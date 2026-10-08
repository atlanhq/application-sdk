# security: Secrets, credentials, injection, isolation
- Flag: hardcoded keys/tokens/passwords/DSNs; real creds in fixtures. Never quote the value.
- Flag: logging credential objects, auth headers, DSNs with passwords, tokens, cookies, JWTs.
- Flag: raw dict credentials instead of `CredentialRef` + `CredentialResolver`; secrets in Temporal Input/Output; new use of `CredentialRef.from_workflow_args()` (deprecated).
- Flag: secret material (`*.pem|key|keytab|p12|jks`, refresh tokens) read through a data object-store binding (`DEPLOYMENT_OBJECT_STORE_NAME`, `UPSTREAM_OBJECT_STORE_NAME`).
- Flag: SQL built by f-string/concat from input (filters go through `validate_filter_no_sql_injection`); `shell=True` with variables, `eval`/`exec`, untrusted `pickle.loads`, `yaml.load` without SafeLoader.
- Flag: caller-controlled path joins without traversal checks; a field skipping the `validate_*` its same-sink siblings use.
- Flag: TLS verification disabled (not the storage integrity `verify` kwarg), wildcard CORS, tracebacks in HTTP responses.
- Flag: keys bypassing the run-scoped layout (`WORKFLOW_OUTPUT_PATH_TEMPLATE`); bodies to Atlan edge routes with PEM or `{{ }}`/`${}` text unencoded (WAF rejects).
- Severity: critical for secret exposure, injection, cross-run access, shared cred/data storage; high otherwise. Never low.
