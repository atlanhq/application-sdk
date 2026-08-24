---
schema: 2
id: APP-SECRETS-001
level: L2
category: security
globs: []
severity: HIGH
suppressible: false
---
# Indirect credential leaks

The conformance suite flags direct credential-in-log values, hardcoded
credentials, and raw env access mechanically. This rule owns the
indirect leaks static analysis cannot recognize:

- Objects whose string form embeds credentials: connection URLs,
  `engine.url`, `connect_args`, headers dicts, `repr()` of any
  client/engine object — in logs, exception messages, or error evidence.
- Credential material in metric label values or activity outputs (it
  persists in workflow history and dashboards).
- Credential VALUES crossing a durable or observable boundary:
  persistence, logs, activity payloads/outputs, or any cross-activity /
  cross-process handoff — pass references there (store path, env name).
  In-process resolution of a reference to a value for client
  construction is normal and NOT a finding.
