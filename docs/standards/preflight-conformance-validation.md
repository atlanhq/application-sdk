# Preflight static validation

Validated existing local connector snapshots without changing app files. Scanned Python files under each app directory. These results are diagnostics, not live workflow or complete behavioral certification. P062–P064 were not executed against these apps; scenario adapters remain necessary.

| Connector | Revision | Python files | Findings |
| --- | --- | ---: | --- |
| mssql | `8315b597fe4e0f48c6c28f5ee858bf29f577abb9` | 25 | P047: 2, P053: 3, P057: 2, P065: 1 |
| powerbi | `901fcf6225a006962be428a5429eab59d96fcd9e` | 64 | P053: 24 |
| redshift | `3ef874affaa83874ee2a6b7129ba2b6b56af4a3e` | 24 | P047: 6, P053: 2 |
| databricks | `ec81bb62a30ab9415ff82bfb3be47eb8ca7039f7` | 175 | P033: 2, P047: 11, P053: 33, P054: 2, P057: 2, P065: 1 |

Static diagnostics include inherited missing suggested actions, expected typed raises, synchronous probes, warning logs, duplicate workflow preflights, and unresolved analysis. Source-specific findings require review before app changes.

Validation: 120 focused preflight tests passed. The full conformance run had 3,515 passes, one expected failure, and two SDK contract-registry failures involving `workflow_slug`. Both failures reproduced using the unchanged baseline test file. Pre-commit checks and generated rule documentation checks passed.
