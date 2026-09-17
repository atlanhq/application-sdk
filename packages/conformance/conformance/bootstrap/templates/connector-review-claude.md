<!-- BEGIN APPLICATION SDK CONNECTOR REVIEW -->
## Mandatory pre-commit review (L2-L4 rules; L1 in PR CI)

Before any `git commit`, run the `connector-review` skill for the applicable
L2-L4 rules. It is local review guidance, not a marker-based commit gate.

L1 conformance runs in CI on every pull-request update; the repository's CI
configuration remains authoritative for enforcement.
Shared L2/L4 rules are fetched into `.mothership/.cache/review-rulesets/` and
L3 rules live in `.mothership/review-rulesets/connector-app/`.
<!-- END APPLICATION SDK CONNECTOR REVIEW -->
