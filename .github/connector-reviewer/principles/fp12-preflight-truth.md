---
id: FP12
applies-to: [app]
---
# Preflight tells the truth

Principle: Preflight answers one question honestly: can the real run do what it is about to do
with this credential and these settings? Every way the app reaches the source has a check that
needs the same permission, asked through the same call the run makes.
Why: A missing grant found hours into extraction wastes the run. One that returns an empty result
instead of an error is worse: the run goes green with assets missing.
Ask:
- Does the diff add or widen source access (an endpoint, scope, SQL statement, system view, client method, credential field) without a preflight check for that permission, added here or named in the PR?
- Does each check ask the way the run does (same call, same object, same privilege), read every page before saying something is absent, and match what the handler really returns?
- Is the needed permission taken from the source's documentation, not guessed from a name? If it cannot be confirmed, ask the author; do not raise a finding.
Don't flag: handler mechanics conformance reports: reserved names and contracts (`F001`, `F006`),
typed failures with actions (`F003`, `F007`), verdicts returned and aggregated (`F008`, `F009`),
blocking probes, budgets and cleanup (`F011`, `F012`, `F013`), leaked text (`F014`), required
scenarios (`F016`). Never ask a handler to raise or block: the gate decides that.
Severity: High when a missing permission gives an empty or partial result under a green run. Medium
when it fails loudly mid-run instead of at preflight.
