---
schema: 2
id: PLATFORM-RUNTIME-001
level: L4
category: runtime
globs: []
severity: HIGH
provenance: application-sdk
suppressible: false
---
# Keep workflows deterministic and activities bounded

When changed code runs in a Temporal workflow, verify that it does not perform
network, file, clock, random, or environment I/O directly. Such I/O belongs in
an activity. For changed long-running activities, verify that timeouts, retries,
and heartbeats are bounded and preserve safe replay.

Do not report this rule when the diff does not change workflow or activity behavior.
