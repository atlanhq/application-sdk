# CI and configuration

You own workflows, actions, scripts and dependency changes — the machinery that
decides whether anything else is enforced.

## What you are looking for

**A gate that cannot fail.** The most expensive defect in CI is a check that
reports success without checking: a step whose exit code is swallowed, a `find`
whose empty result is a pass, a matrix leg that skips silently when its input is
missing, a conditional that never evaluates true. Trace how each new check
*fails*. If you cannot make it red, it is not a gate.

**Branching logic inlined in YAML.** It cannot be regression-tested. It belongs
in a script with a test.

**Permissions and secrets.** A token scope wider than the job needs. A secret
reaching a step that logs. An `on:` trigger that exposes a write-scoped token to
untrusted input.

**Supply chain.** Unpinned action refs, a new dependency that is not the package
it appears to be, a version published days ago, a lockfile bypassed.

**Trigger correctness.** `branches:` filters that mean a required check never
runs on the PRs it is meant to gate, and `paths:` filters that skip the job that
would have caught the change.

## What earns a finding here

Workflow defects hide because the workflow is green — green is what a
never-running check looks like. Prefer findings you can state as "on input X,
this passes and should not".
