# SDK Review — Learned-not-to-flag log

> A running record of findings that sdk-review must NOT raise. Entries
> arrive from two sources: (1) `@sdk-review challenge` flows where
> Claude withdraws a finding and emits a retrospective YAML block; (2)
> explicit team guidance recording "do not flag this pattern".
>
> **Purpose**: teach future reviews to avoid re-flagging the same
> false-positive pattern. When running a fresh `@sdk-review`, the
> session consults this log as part of its rule set — if a candidate
> finding matches a `do_not_flag` pattern here, the finding must be
> withdrawn silently (no inline comment, no auto-fix, no removal
> request).

---

## Entries

```yaml
- pattern: "@pytest.mark.asyncio decorator on test functions"
  do_not_flag: true
  added: 2026-05-20
  source: team-guidance
  reason: |
    Whether to use the @pytest.mark.asyncio decorator alongside
    pytest's asyncio_mode="auto" is a team style preference, not a
    correctness issue. The decorator does not break anything and
    several team members prefer it for explicitness.

    Earlier guidance treated this as redundant/forbidden. That
    guidance has been retracted by the team. sdk-review must NOT
    surface it as a finding, inline comment, or auto-fix.
  applies_to:
    - "@pytest.mark.asyncio"
    - "@pytest.mark.asyncio(...)" # with parameters
    - "pytest.mark.asyncio" # in usefixtures / similar
  detection_to_skip:
    - "grep -E '@pytest\\.mark\\.asyncio' diff"
```
