---
name: remediate
description: Drive the conformance remediation loop (validators + OpenProse programs from the atlan-application-sdk-conformance package)
argument-hint: "[--area error-handling|logging|ci] [--strict] [path]"
---

1. Resolve programs dir:
   - Inside a connector repo: `PROGRAMS=$(uv run atlan-application-sdk-conformance programs-dir)`
   - Anywhere else: `PROGRAMS=$(uvx atlan-application-sdk-conformance@latest programs-dir)`
2. Read `$PROGRAMS/conformance-remediation.prose.md` and execute it as the entry contract.
3. All gated re-checks call `atlan-application-sdk-conformance detect` — follow the .prose.md exactly.
