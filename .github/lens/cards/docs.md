# docs: Documentation changes
- Flag: docs contradicting code: wrong import path, signature, default, env-var name or command. Verify with `find_symbol`/`search_code` first.
- Flag: examples using removed/deprecated APIs (`application_sdk.workflows|activities|handlers`, `ObjectStore`, `ParquetFileWriter`/`JsonFileWriter`, `call()`/`call_by_name()`), `datetime.now()`/`uuid4()` in `run()`, raw dict credentials, or bare `AppError`.
- Flag: examples that would be defects in code: hardcoded secrets, `verify=False`, f-string SQL, mutable contract defaults, missing `from e`.
- Flag: customer names, tenant names, run IDs or incident IDs (use "a production incident").
- Flag: a new env var/config key documented without purpose, default and who sets it; removed/renamed env vars still documented by the old name.
- Flag: a concept doc (`docs/concepts/*.md`) describing behaviour the same PR's code changes differently.
- Flag: relative links or anchors to files/sections that do not exist.
- Don't flag: prose style, wording, formatting, CHANGELOG.md.
- Severity: high if docs teach an insecure or broken pattern; medium for drift; low otherwise.
