---
id: FP07
applies-to: [app, contract]
---
# Nothing grows with the customer's source size

Principle: What a run holds in memory, passes between tasks, records in workflow history or keeps
open has a bound set by design, not by how much the source contains. Bulk data travels as files
behind a `FileReference`, and the worker sizing in the contract fits what the code allocates.
Why: Unbounded things pass in development and fail on the largest customer: an out-of-memory kill,
a 2 MB payload error or a full workflow history, usually reported as an unrelated timeout.
Ask:
- Does anything recorded in workflow history grow with the source: the size of a task output, workflow result or signal, or the number of tasks, batches and retries?
- Does a task hold a whole result set, an unbounded cache, an embedded engine with no memory limit and spill directory, or a client opened per item?
- If the change raises memory per task, fan-out or local disk use, or edits worker sizing, does concurrency times per-task memory still fit the pod limit, and does the PR say so?
Don't flag: contract shapes conformance reports: the unbounded-fields opt-out (`P001`), bytes and
path strings (`P011`, `P012`), untyped or JSON-text fields (`P015`, `P059`). It also reports
unclosed SDK clients (`P056`, `P057`), batched results read on the loop (`P065`) and hand-rolled
disk lookups (`O006`). Do not flag collections with a small fixed bound.
Severity: High when a large source would fail the run or the worker. Medium for growth that is
real but far from any limit at today's sizes.
