---
id: FP02
applies-to: [app, contract]
---
# Asset identity does not change by accident

Principle: A qualified name is the asset. For the same source object, connection and settings, a
run after this change produces the same qualified name, type and parent links as a run before it,
unless the PR sets out to change them and says what happens to assets already published.
Why: A changed string is a new asset. Downstream the old asset is deleted and a new one created,
everything linked to the old one is orphaned, and a collision makes two source objects overwrite
each other.
Ask:
- Does the diff change anything that feeds a qualified name or other unique key: a segment, its order, case, escaping, trimming, a number or date format, a default, or the field it is read from?
- Can two source objects now get the same name, or one object a different name on the next run (unordered input, a hash fallback, a blank or missing segment)?
- If identity changes on purpose, does the PR name the asset types affected and what happens to the existing assets?
Don't flag: how a name is built when the resulting string is unchanged. Conformance reports
hand-built names (`P028`), raw-dict entities (`P058`), serialisation outside `entity_bytes`
(`P052`), relationship direction (`P055`) and strict parsing of the connection qualified name
(`P049`). Do not flag new asset types or new attributes on existing assets.
Severity: High when assets already published for a connection would be renamed, merged or dropped.
Medium for a new asset type whose name can collide or is not stable from run to run.
