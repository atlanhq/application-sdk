---
id: FP11
applies-to: [app, tests]
---
# Credentials stay on the credential path

Principle: A secret value lives in one place: in memory, between the SDK resolving a credential
reference and the client built from it. Everything else (logs, errors, metrics, task inputs and
outputs, files, test fixtures) carries a reference or a name, never the value or an object that
prints it.
Why: Logs, workflow history, dashboards and git are kept for a long time and read by far more
people than the secret store. One leaked value means rotating a customer's credential.
Ask:
- Does an object whose text form holds a secret (a connection URL, `engine.url`, `connect_args`, a headers dict, a client or engine `repr`) reach a log, an error message or error evidence?
- Does a secret value, or anything built from one, cross a task boundary, reach a file or become a metric label, where a reference should travel instead?
- Do new tests, fixtures or recorded responses contain a real token, key, password or customer host?
Don't flag: direct leaks conformance reports: a credential value in a log call (`L010`), hard-coded
credentials (`S001`), credential env vars read directly (`S002`), secret-named error evidence
(`E017`), raw exception text in messages and preflight output (`E015`, `E019`, `F014`), own
credential routing (`P053`). Resolving a reference in memory to build a client is normal.
Severity: High for any path that can write a real secret to a log, history, a metric, a file or the
repository. Medium for a risky pattern that carries no secret yet.
