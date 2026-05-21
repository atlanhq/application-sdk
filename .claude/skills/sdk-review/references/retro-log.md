# SDK Review — Learned-not-to-flag log

> A running record of findings that were **withdrawn** after `@sdk-review challenge`
> pushback, with the author's objection and Claude's reason for agreeing.
>
> **Purpose**: teach future reviews to avoid re-flagging the same false-positive
> pattern without strong new evidence.
>
> **Format**: each entry is a YAML block appended by the challenge flow
> (`@sdk-review challenge: <reason>` → when Claude decides a finding should be
> withdrawn, the retrospective YAML block is emitted in the challenge response
> comment). For v1, entries are aggregated here manually from those comments.
> A future automation can parse the YAML blocks and append automatically.
>
> When running a fresh `@sdk-review`, the session is instructed to consult this
> log as part of its rule set — if a candidate finding matches a withdrawn
> pattern here, require **additional evidence** before raising it.

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
