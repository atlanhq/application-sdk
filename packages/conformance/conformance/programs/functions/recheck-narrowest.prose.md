---
kind: function
name: recheck-narrowest
description: >
  Re-runs the conformance suite scoped to a single file and returns whether a
  specific finding is now clear.  Deterministic — the suite runner is the only
  permitted judge.
---

### Parameters

- `scope` (string, required) — repository root path.
- `file` (string, required) — repo-relative path of the file that was just
  edited (e.g. `application_sdk/app/base.py`).
- `rule_id` (string, required) — the rule that was fixed or suppressed,
  e.g. `"E002"`.
- `fingerprint` (string, required) — the `partialFingerprints["atlanConformance/v1"]`
  value from the original finding; used to confirm the exact finding is gone,
  not merely that the file has no other violations of the same rule.

### Returns

- `clear` (boolean) — true if the finding identified by `fingerprint` no
  longer appears in the suite output with a FAILING or WARNING disposition
  after the edit.
- `new_violations` (list) — any net-new FAILING findings in the file that were
  not present before (guard against fix-introduces-different-violation).

### Implementation

Derive the series letter from the first character of `rule_id`.

```sh
uv run atlan-application-sdk-conformance detect \
  --repo <scope> \
  --series <series_letter> \
  --output - 2>/dev/null
```

Parse the SARIF output.  Filter results to those whose
`locations[0].physicalLocation.artifactLocation.uri` matches `file`.

`clear` is true when no result with `partialFingerprints["atlanConformance/v1"]
== fingerprint` has a FAILING or WARNING disposition.

`new_violations` is the list of FAILING findings in `file` whose fingerprints
were not present in the pre-edit run (caller passes them; if unknown, treat as
empty and log a note that net-new check was skipped).

For `mechanism=static` rules (all E-series) this runs in milliseconds and
requires no built environment.
