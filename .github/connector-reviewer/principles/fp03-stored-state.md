---
id: FP03
applies-to: [app]
---
# Stored state has one owner and is never half-written

Principle: Every stored thing (an output file, an object-store prefix, a marker or watermark) is
written by one task, under a directory that task owns, and appears at its final name only when
complete. Other tasks receive it through the writer's declared output, never from shared disk or a
listed prefix.
Why: Tasks run on different pods and are retried. A reader that trusts local disk, a half-written
file or a dead attempt's leftovers publishes partial data as if it were complete.
Ask:
- Does a task read a file or prefix it did not write in this attempt, other than through a `FileReference` on its input?
- If this task is cancelled or retried half-way, what does the next attempt or the next task find: a dead attempt's files, a cut-off file at the real name, a marker ahead of its data?
- Is each marker or watermark advanced by one owner, and only after the data it describes is safely stored?
Don't flag: what the SDK owns: `FileReference` upload and cleanup after a task returns, SDK
writers, `App.upload()` from `run()`. Conformance reports app state used between tasks (`P027`),
transfers inside a task (`P008`, `P044`), own storage clients (`P009`), path strings and hand-built
references in contracts (`P012`, `P010`), and a hand-built persistent prefix (`P048`).
Severity: High when a retry, a second pod or a cancelled attempt can make a run publish partial or
stale data. Medium for local files left behind, or a non-atomic write that nothing else reads.
