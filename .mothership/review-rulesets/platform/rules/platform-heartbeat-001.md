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

- SDK `@task` auto-heartbeat runs on the event loop. Any code path that
  blocks the loop stops heartbeats and the activity is killed mid-work.
- MUST heartbeat explicitly inside long synchronous sections: work offloaded
  with `asyncio.to_thread` and tight parse/transform loops do NOT yield to
  the loop on their own.
- MUST configure a heartbeat on every long-running activity; an activity
  that neither heartbeats nor offloads cannot be distinguished from a hang.
- MUST NOT "fix" a heartbeat timeout by raising the timeout value — find the
  blocking section.
