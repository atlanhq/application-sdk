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

<!-- No entries yet. The first entries will appear after the challenge
     command is used and Claude withdraws at least one finding. -->
