---
id: FP04
applies-to: [app]
---
# Workflows orchestrate; tasks do the work

Principle: Code in `run()` and `@entrypoint` methods only decides what runs next: it awaits tasks,
timers and signals and passes their typed outputs on. All I/O, clocks, randomness and anything read
from the environment happens inside a `@task`.
Why: The workflow body is replayed from history after a restart. Anything in it that cannot be
replayed gives a different answer the second time, or waits on something that never arrives, and
the run hangs until its timeout instead of failing.
Ask:
- Follow each new `await` in a workflow body to its end: does it reach a task, timer or signal, or a helper that touches storage, HTTP, the file system or a client?
- Does the body work out a run-scoped value itself (an output path, a marker, a setting) instead of taking it from a task's output?
- Does the diff add an `imports_passed_through()` block or another sandbox escape with no written reason?
Don't flag: direct calls in workflow code, which conformance reports: clocks, uuid, sleep and
randomness (`P020`), file, network, env and process I/O (`P021`), coroutines never awaited
(`P022`), `temporalio` and private orchestration imports (`P004`, `P005`). Do not flag helpers that
only compute on their arguments.
Severity: High when a workflow can hang or diverge on replay in production. Medium when the helper
is harmless today but nothing stops it from gaining I/O later.
