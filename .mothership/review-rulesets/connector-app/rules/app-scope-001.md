---
schema: 2
id: APP-SCOPE-001
level: L2
category: correctness
globs: []
severity: MEDIUM
suppressible: true
---
# A fix for one server's quirk must not change every connection

- FLAG a diff that changes a class-level or module-level client default
  (`DB_CONFIG`, pool/engine options such as `pool_pre_ping`, `connect_args`,
  a shared constant) to work around one server that behaves differently,
  when the connector also serves servers that behave normally. The change
  reaches every connection the app opens, with no per-connection opt-out
  and no stated reason the default is unsafe for every server.
- Safe path: a per-connection setting (a credential extra or form field)
  applied to that client only, leaving the class default untouched; or a PR
  that states why the default is wrong for every supported server.
- Does not apply when the app serves only the engine that needs the change.
