---
schema: 2
id: PLATFORM-HEARTBEAT-001
level: L4
category: platform-runtime
globs: []
severity: HIGH
suppressible: false
---
# Heartbeats must survive synchronous work

- SDK auto-heartbeat runs on the event loop; any code path that blocks the
  loop stops heartbeats and the activity is killed mid-work.
- Work offloaded to a thread MUST heartbeat explicitly inside the loop body
  and carry its own internal timeout — the framework cannot safely kill a
  thread, and a thread does not yield to the loop on its own.
- Tight parse/transform loops inside async generators do not yield either;
  insert explicit heartbeat/yield points on long iterations.
- MUST NOT "fix" a heartbeat timeout by raising the timeout value — find
  the blocking section. Timeouts sized to the p99 of real work, stated in
  the PR when changed.
