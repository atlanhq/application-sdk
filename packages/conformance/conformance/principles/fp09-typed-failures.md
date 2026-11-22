---
id: FP09
applies-to: [app]
---
# Failures are typed and honest

Principle: A failure leaves the code as a typed error whose category, audience and retry verdict
say what really happened and who must act. A failure is never turned into a value that looks like
success, and a failure the code handled is never reported as a failed run.
Why: The platform retries, routes and displays failures from these fields alone. A wrong verdict
kills a multi-hour run on its first error, retries a hopeless one, or sends a customer's problem to
the wrong team.
Ask:
- Do the category and audience match what happened and who can fix it (for example, a source that never answered is source unavailable, not a timeout or a platform fault)?
- Is "retry this" decided separately from "this may be absorbed as a declared gap", on a stable code or type rather than message text, with unknown source errors left retryable?
- After a catch, can the caller still tell failure from success, and can a re-raise ever make an error its own cause?
Don't flag: mechanics conformance reports: bare, broad or silent excepts (`E001`, `E002`, `E004`,
`E006`, `E014`), a value returned from an `except` (`E007`), untyped or legacy raises (`E012`,
`E013`, `E018`), missing chaining (`E016`), exception text in messages (`E015`, `E019`), taxonomy
overrides (`P002`, `P003`, `E021`), log level and duplicate logging (`L022`, `L009`).
Severity: High when a wrong verdict fails healthy runs, hides a failure as success, or retries
without end. Medium when the failure is visible but labelled or routed wrongly.
