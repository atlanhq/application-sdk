---
id: FP17
applies-to: [app]
---
# What customers and operators see is accurate and bounded

Principle: Every name, description, message, log line and metric a customer or operator reads says
what is true now, in words they can act on. Its volume and its set of label values do not grow
with the source.
Why: People decide what to do from these surfaces. Internal jargon or a stale description sends
them the wrong way, and an unbounded label or a log line per item buries the signal and overloads
the metrics store.
Ask:
- Is each customer-visible string (display name, description, error message, suggested action) free of internal names, consistent with sibling connectors, and clear about what the reader can do?
- Can a metric label or a repeated log line take a new value per run, asset or error (ids, counts, exception text), or does the diff add a diagnostic loop that never stops?
- Does anything near the diff now say what the code no longer does: a comment or docstring, a hard-coded list of source names or types, or a setting parsed but never used where the client is built?
Don't flag: log mechanics conformance reports: f-strings and concatenation (`L001`, `L011`),
`info` in a tight loop (`L006`), `print` (`L005`), log level (`L022`), preflight messages and
actions (`F007`), placeholders left in text (`O005`), form keys that never reach the run (`K019`).
Do not flag wording taste, or comments far from the diff.
Severity: High when a customer is told something false, or an unbounded label can overload the
metrics store. Medium for jargon, stale comments and noisy logs.
