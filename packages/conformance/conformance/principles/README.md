# Fleet principles for the connector reviewer

The connector reviewer is an AI reviewer for pull requests in connector app repos
(`atlan-*-app`). It judges a pull request against the principles in this directory.
They ship inside this package, so CI and a coding agent on a laptop read the same copy:

```bash
uv run atlan-application-sdk-conformance principles-dir
```

## What a principle is

A principle is one short file that says:

- one thing that must stay true in a connector app;
- the production failure it prevents;
- up to three questions the reviewer answers about the diff;
- what the reviewer must leave alone;
- what makes a violation high severity.

A principle is for the judgement that code cannot make: whether a run can lose data
without saying so, whether a test would really catch a regression, whether a fix for
one server changes behaviour for every customer.

## What a principle is not

- **Not a conformance rule.** If code can check it, it belongs in the conformance
  suite or another CI gate. The reviewer never repeats what a gate already enforces.
  The `Don't flag:` field of each principle names the conformance rules that cover
  its mechanical side.
- **Not a checklist of patterns.** It states what must hold and why, then asks
  questions. A pattern list goes stale and is matched without thought.
- **Not style guidance.** Naming, formatting and taste are out of scope.

## Two levels

| Level | Where it lives | Ids | What it covers |
|---|---|---|---|
| Fleet | this directory | `FP01` onwards | every connector app |
| Repo | `.github/connector-reviewer/principles/` in the app repo | `RP01` onwards | what is true only for that app |

Both levels use the same format. Put a principle at the repo level when it is about
one source or one app's design. Put it here only when it holds for every connector.

## The fleet principles

| Id | Principle | Areas |
|---|---|---|
| FP01 | Never publish undeclared loss | app |
| FP02 | Asset identity does not change by accident | app, contract |
| FP03 | Stored state has one owner and is never half-written | app |
| FP04 | Workflows orchestrate; tasks do the work | app |
| FP05 | Async first; blocking work is bounded and can be cancelled | app |
| FP06 | Long work reports progress | app |
| FP07 | Nothing grows with the customer's source size | app, contract |
| FP08 | Contracts only grow | contract, generated, app |
| FP09 | Failures are typed and honest | app |
| FP10 | Use the SDK's mechanism; don't rebuild it | app |
| FP11 | Credentials stay on the credential path | app, tests |
| FP12 | Preflight tells the truth | app |
| FP13 | One source's quirk stays scoped to that source | app |
| FP14 | A change comes with a test in the right tier | app, tests |
| FP15 | A changed test still proves the behaviour | tests |
| FP16 | A change that undoes a recent change says why | everywhere |
| FP17 | What customers and operators see is accurate and bounded | app |

## Format

```markdown
---
id: FP01
applies-to: [app, contract]
---
# Never publish undeclared loss

Principle: <one or two sentences stating what must stay true>
Why: <one sentence: the production failure this prevents>
Ask:
- <a question the reviewer should answer about the diff>
- <at most three bullets>
Don't flag: <what conformance or CI already enforces, with rule ids in backticks such
as `E020`, and anything else that is out of scope>
Severity: <what makes a violation high; otherwise medium>
```

The rules, which `conformance.principles.load()` enforces:

- The file is named `<id in lower case>-<slug>.md`, for example
  `fp01-undeclared-loss.md`.
- Front matter sits between two `---` lines. `id` is required: `FP` (fleet) or `RP`
  (repo) followed by two digits. `applies-to` is optional: an inline list of areas.
  Leave it out when the principle applies everywhere. No other keys.
- Then an H1 title and a blank line.
- Then the five fields in this order, each label at the start of a line:
  `Principle:`, `Why:`, `Ask:`, `Don't flag:`, `Severity:`. No blank lines between
  them.
- `Ask:` stands alone on its line and is followed by one to three `- ` bullets, one
  line each.
- The other four fields are one paragraph each. A paragraph may wrap over lines.
- A file is at most 26 lines, front matter included.
- Write short, plain sentences. No emojis. No labels that need a glossary.
- Cite a conformance rule by its id in backticks. A test checks that every cited id
  exists in the rule catalog and runs on app repos.

## Severity

A finding is high or medium. Each principle says what makes its violations high. Two
things hold across all of them:

- Losing data without saying so outranks failing loudly. A stack trace is seen;
  missing assets are not.
- The more customers a change reaches, the higher the severity. The same defect in
  code every connection runs outranks it in one rarely used path.

## Areas

`applies-to` takes area names only, never paths or globs.

| Area | Files |
|---|---|
| `app` | `app/`, except generated code |
| `generated` | `app/generated/` |
| `tests` | `tests/` |
| `contract` | `contract/` |
| `ci` | `.github/` |

## Reading them in code

```python
from conformance import principles

for p in principles.load(principles.FLEET_DIR):
    print(p.id, p.title, p.areas, p.ask)
```

`load()` takes any directory in this format, so it reads a repo's own principles too.
It uses the standard library only. A file that breaks the format raises `ValueError`
naming the file and the problem. `Principle.areas` is empty when the file has no
`applies-to`; `Principle.applies_to(area)` answers for both cases.

## Adding or changing a principle

1. Ask first whether code could check it. If so, write a conformance rule instead.
2. Ask whether it holds for every connector. If it is about one app, it is a repo
   principle and belongs in that app's repo.
3. Edit the file, or add one with the next free id. Never reuse or renumber an id:
   the id is how everything else refers to the principle.
4. Read the conformance rule catalog (`conformance/suite/rules/`) and name in
   `Don't flag:` every rule that already covers part of the principle.
5. For a new fleet principle, add it to the table above and to the expected ids in
   `tests/test_principles.py`.
6. Run `uv run --project packages/conformance pytest packages/conformance/tests/test_principles.py`.

A change reaches CI and local agents with the next release of this package.

## Where the 25 old shared rules went

The principles replace the two shared rule sets under `.mothership/review-rulesets/`
(`connector-app`, 18 rules; `platform`, 7 rules). FP02 and FP16 are new and have no
old rule behind them.

| Old rule | Where it went | Note |
|---|---|---|
| `APP-CORRECTNESS-001` | FP01 | |
| `APP-CONFORMANCE-001` | Dropped | The conformance gate reports its own findings; the reviewer does not repeat them. |
| `APP-ERRORS-001` | FP09 | |
| `APP-STORAGE-001` | FP03 | |
| `APP-RESOURCES-001` | Moved to a deterministic check; remainder in FP07 | Conformance reports unclosed SDK clients (`P056`, `P057`) and preflight cleanup (`F013`). The judgement part, a client built per call or per item, is in FP07. Its other shapes (a name first set inside `try` and read in `finally`; cleanup that can raise inside `finally`) are mechanical, have no conformance rule yet, and are not reviewer work. |
| `APP-SECRETS-001` | FP11 | |
| `APP-OBSERVABILITY-001` | FP17 | |
| `APP-TESTS-001` | FP15 | Kept light on purpose: FP15 looks only at an existing expectation that was weakened or removed. A new test that cannot fail without the change is caught by FP14's first question. |
| `APP-TESTS-002` | FP14 | |
| `APP-TESTS-003` | FP14 | |
| `APP-TESTS-004` | FP14 | |
| `APP-SDK-DRIFT-001` | FP10 | |
| `APP-METADATA-001` | FP17 | |
| `APP-STALENESS-001` | FP17 | |
| `APP-VALIDATION-001` | FP12 | |
| `APP-PREFLIGHT-001` | FP12 | |
| `APP-SCOPE-001` | FP13 | |
| `APP-GATES-001` | Moved to a deterministic check | The reviewer's gate-file check (planned). Whether a pull request touches a file that judges it is a fact about the diff, so code decides it, not the model. |
| `PLATFORM-RUNTIME-001` | FP04 | |
| `PLATFORM-PAYLOAD-001` | FP07 | Its line on credentials in payloads is in FP11. |
| `PLATFORM-HISTORY-001` | FP07 | |
| `PLATFORM-HEARTBEAT-001` | FP05 | Progress signals and holds are in FP06. |
| `PLATFORM-MEMORY-001` | Moved to a deterministic check; remainder in FP07 | Conformance reports batched results read on the event loop (`P065`) and hand-rolled disk lookups (`O006`). Whole-result loads and an embedded engine with no memory limit have no conformance rule yet, so FP07 still asks about them, along with unbounded caches. |
| `PLATFORM-CONTRACT-001` | FP08 | |
| `PLATFORM-RESOURCES-001` | FP07 | |
