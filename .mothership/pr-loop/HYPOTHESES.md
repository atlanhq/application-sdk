# Break it

Before you judge whether this change is correct, try to make it wrong. Write
down how it breaks, then turn each way into a test. This is the one part of the
review where being imaginative is the job.

## Write the hypotheses down first

Produce a file, `.sdk-loop/hypotheses.md`, before you write any test. Committing
to the list first is what stops the exercise collapsing into tests for the paths
you already know pass — the failure mode of every test suite written after the
code it tests.

Each hypothesis is one line: **the input or condition, and what you expect to go
wrong.**

```
- concurrent entry: two activities call `resolve()` on the same key; the second
  overwrites the first's cached credential
- boundary: `page_size=0` makes the paginator loop forever rather than return
  empty
- partial failure: the upstream returns 200 with a truncated body; the parser
  reports success over half a record
```

Not this:

```
- test that resolve() works
- check error handling
```

## Where to look

The classes below are where defects in this SDK actually live. Work through
them against the changed code; most will not apply, and saying so is fine.

- **Boundaries** — empty, one, exactly the limit, one past it, zero, negative,
  null, the maximum the type holds.
- **Concurrency** — two callers at once, the same caller re-entering, a retry
  overlapping its own first attempt, shared mutable state across activities.
- **Partial failure** — the call that succeeds halfway: a write that lands then
  raises, a batch where record 3 of 10 fails, a connection dropped mid-stream.
- **Hostile input** — malformed upstream payloads, unexpected nulls, encodings,
  a field that is a string where the contract says integer.
- **Exhaustion** — retries used up, memory on a large page, a timeout expiring
  while a lock is held.
- **Replay** — for anything in a workflow method: what happens when this code
  runs a second time against the same history?

## Then write the tests

One test per hypothesis that survives contact with the code. Each must fail
against the *unfixed* source — that is the point, and the runner checks it.

A test you cannot make fail against the old code has not captured this change.
It may still be worth keeping, but do not count it as covering the fix.

## What to report

Hypotheses that the code already handles are not findings — note them in
`strengths` if the handling is non-obvious. A hypothesis you could not resolve
either way is an honest `NEEDS_HUMAN` input, not a guess. A hypothesis that
breaks the code is a finding, and it arrives with its own reproduction, which
makes it the most useful kind you can file.
