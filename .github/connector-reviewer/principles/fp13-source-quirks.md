---
id: FP13
applies-to: [app]
---
# One source's quirk stays scoped to that source

Principle: A workaround for one server, version, account type or customer setup applies only where
that condition holds. It does not change a default that every connection of the app uses.
Why: A shared default changed for one odd server changes behaviour, load or results for every
customer whose source behaves normally, and none of those were tested.
Ask:
- Does the diff change a class-level or module-level default (client or engine options, pool settings, `connect_args`, a timeout, a shared constant) to fix a problem seen on one source?
- Could it be a per-connection setting, or a branch on the detected version or capability, that leaves the default alone?
- If the default really is wrong for every supported source, does the PR say why?
Don't flag: an app that serves only the one engine or product that needs the change; a
per-connection option added without touching the default; a default changed with a stated reason
that holds for every supported source. No conformance rule or CI gate checks this, so there is
nothing to leave to them.
Severity: High when the changed default can lose data, break sign-in or multiply load for other
customers. Medium when it changes behaviour or speed in a way they would notice.
