"""Credential-seam rule definition (P053, FND-2949).

A connector input carries up to four credential channels: a pre-built
``CredentialRef`` field (the generic ``credential_ref``, or the
``<app>_credential`` field the contract toolkit generates), ``credential_guid``,
``agent_json``, and inline ``credentials`` pairs.  Turning those into one
``(credential_ref | None, inline_credentials)`` pair is SDK-owned:
``application_sdk.credentials.route_credentials`` does it, and
``AppContext.resolve_credential_raw_or_inline`` reads the result on the task
side.

Why this rule exists
--------------------
Before that seam existed, connector apps each hand-rolled a
``build_credential_ref(input)`` and the copies drifted on every axis the SDK now
decides once:

* **routing** — some call ``CredentialRef.resolve`` (agent-aware), one builds
  ``CredentialRef(credential_guid=...)`` directly and so never routes
  ``agent_json`` at all, one calls the lenient ``resolve_or_none``;
* **the inline shape** — each flattens ``[{key, value}]`` pairs into a dict its
  own way, and each declares its own ``CredentialValue`` / bounded-dict alias to
  type the result.

The same workflow input therefore resolves to different credentials depending on
which app received it, and the difference only surfaces on the input shape a
given app's fixtures never exercise (an agent-mode run, an inline pair with a
missing ``value``).

What is detected
----------------
"Re-implements the router" is not statically decidable, so the rule keys off the
fingerprints the local copies leave:

* **routing** — app code that calls ``CredentialRef.resolve(...)`` /
  ``CredentialRef.resolve_or_none(...)`` or constructs ``CredentialRef(...)``
  whose ``credential_guid=`` is the input's own GUID channel
  (``input.credential_guid``, ``args.get("credential_guid")``, or a local bound
  from one).  That is app code deciding the route itself; a GUID taken from some
  other field names a second credential, not the input's channel, and stays
  silent.  A ``CredentialRef(name=..., credential_type=...)`` for a named
  secret carries no ``credential_guid=`` and is not routing, so it stays
  silent.  App code flattening inline ``[{key, value}]`` credential pairs
  itself counts too — it is the only fingerprint a dict-based router over raw
  ``workflow_args`` leaves.  One finding per function, anchored at its first
  site.
* **local credential types** — a module-level alias named ``CredentialValue``,
  ``CredentialMap``, ``InlineCredentials`` or ``Bounded*Credential*`` whose value
  is a type union or a (possibly ``Annotated``) dict — the app's own copy of the
  types the seam exports.

Scope
-----
``app``.  The SDK *is* the router: ``application_sdk/credentials/routing.py``
calls ``CredentialRef.resolve`` and defines ``CredentialValue`` by definition.
The runner drops out-of-scope findings (``runner._rule_in_scope``).

SDK-version gate
----------------
The remedy — ``route_credentials`` and its types — first ships in
application-sdk 3.40.0.  Prescribing it to an app pinned below that would name an
import that does not exist, so the check reads the SDK version the app's
``uv.lock`` resolves (the same reader ``P051`` uses) and stays silent unless it
can confirm >= 3.40.0.  No lock, no SDK in it, or an unparseable version: silent.
The gate lives at the repo boundary (``scan_all`` / ``scan_path``); ``scan_text``
is the pure AST pass.

Tier
----
``WARN``.  Every hit is an existing, working copy in another repo that needs a
migration, not a merge block on this one.  Promotion belongs in a later,
evidence-based pass once the fleet has moved.

P-ids are a permanent public contract (see ``prescriptions.py``).
"""

from __future__ import annotations

from conformance.suite.schema.catalog import (
    RemediationKind,
    RemediationReference,
    RuleDefinition,
)
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P053",
        canonical_reference=(
            "atlan-mysql-app app/mysql.py — `run()` takes its ref from "
            "`self.resolve_credential_ref(input)`, the SqlApp template's routing seam, "
            "and never constructs or resolves a CredentialRef itself. An App that is "
            "not a SqlApp has no template method to call; its seam is "
            "application_sdk/credentials/routing.py — `route_credentials(input)` "
            "returns `ResolvedCredentials(ref, inline)`, and "
            "`AppContext.resolve_credential_raw_or_inline(ref, inline)` reads it on "
            "the task side."
        ),
        scope=RuleScope.APP,
        name="LocalCredentialRouting",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="credential-seam",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.40.0",
        rule_interactions=(
            "P037 (SdrAgentJsonNotConsumed) predates this rule and names "
            "`CredentialRef.resolve` as its agent-aware fix, citing a hand-rolled "
            "`build_credential_ref` as its reference. On an SDK below 3.40.0 that is "
            "still the right fix and this rule is silent; from 3.40.0 this rule "
            "flags that call and P037 also accepts `route_credentials` as "
            "agent-aware, so migrating onto the seam clears both rather than trading "
            "one finding for the other. P053's draft for a "
            "`CredentialRef(credential_guid=...)` site is that migration, so it is "
            "also the fix for a P037 finding at the same site."
        ),
        rationale=(
            "Turning a workflow input's credential channels (a pre-built "
            "CredentialRef field, credential_guid, agent_json, inline credentials) "
            "into one (ref, inline) pair is SDK-owned: "
            "application_sdk.credentials.route_credentials. Apps that hand-roll it "
            "drift — one skips agent routing entirely, one resolves leniently, each "
            "flattens inline pairs its own way — so the same input resolves to "
            "different credentials depending on which app received it, and the "
            "difference surfaces only on the input shape that app's fixtures never "
            "exercise (an agent-mode run, an inline pair with no value)."
        ),
        short_description=(
            "App routes credential channels itself, or declares its own "
            "credential types, instead of using the SDK's route_credentials"
        ),
        full_description=(
            "App code decides how a workflow input's credential channels become a\n"
            "credential — or declares its own copy of the types that carry the\n"
            "result — instead of using the SDK's credential seam.\n"
            "\n"
            "Two shapes fire, and the finding says which:\n"
            "\n"
            "* **routing** — a function calls ``CredentialRef.resolve(...)`` or\n"
            "  ``CredentialRef.resolve_or_none(...)``, constructs\n"
            "  ``CredentialRef(...)`` whose ``credential_guid=`` is the input's own\n"
            "  GUID channel (``<x>.credential_guid``, ``<x>['credential_guid']``,\n"
            "  ``<x>.get('credential_guid')``, or a local name bound from one), or\n"
            "  flattens inline ``[{key, value}]`` pairs itself (a loop or\n"
            "  comprehension over a ``cred``-named iterable reading ``item['key']``\n"
            "  and ``item['value']`` / ``item.get('value')``).  One finding per\n"
            "  function, at its first such site, naming every shape it has;\n"
            "* **local credential types** — a module-level alias named\n"
            "  ``CredentialValue``, ``CredentialMap``, ``InlineCredentials`` or\n"
            "  ``Bounded*Credential*`` whose value is a type union or a (possibly\n"
            "  ``Annotated``) ``dict``.\n"
            "\n"
            "Use the seam instead::\n"
            "\n"
            "    from application_sdk.credentials import route_credentials\n"
            "\n"
            "    ref, inline = route_credentials(input)\n"
            "\n"
            "``route_credentials`` prefers a pre-built ``CredentialRef`` field\n"
            "(the toolkit-generated ``<app>_credential``; an input with several\n"
            "names the run's one with ``run_credential_field: ClassVar[str]``,\n"
            "which the preflight gate reads too), routes ``credential_guid`` /\n"
            "``agent_json``\n"
            "through ``CredentialRef.resolve``, and normalises inline\n"
            "``credentials`` into one flat, dotted-key ``CredentialMap``.  On the\n"
            "task side, ``self.context.resolve_credential_raw_or_inline(ref,\n"
            "inline)`` reads either path through one parser.  Type contract fields\n"
            "with ``CredentialValue`` / ``CredentialMap`` / ``InlineCredentials``\n"
            "from ``application_sdk.credentials`` rather than a local alias.  A\n"
            "``SqlApp`` subclass already has a seam for the ref alone:\n"
            "``self.resolve_credential_ref(input)``.\n"
            "\n"
            "Local copies are not merely duplication.  The fleet's copies disagreed\n"
            "on whether ``agent_json`` is routed at all (one built\n"
            "``CredentialRef(credential_guid=...)`` directly), on strict versus\n"
            "lenient resolution, and on how an inline ``[{key, value}]`` list is\n"
            "flattened — so an input resolved differently depending on which app\n"
            "received it, and only on the shape that app's fixtures never used.\n"
            "\n"
            "Not flagged: a ``CredentialRef(name=..., credential_type=...)`` for a\n"
            "named secret (no ``credential_guid=``) — that names a credential, it\n"
            "does not route one; a ``CredentialRef(credential_guid=...)`` built from\n"
            "some other field (a second, per-source credential such as\n"
            "``input.cloud_source``), which is not one of the input's channels; a\n"
            "``CredentialRef(agent_spec=...)``; an import of\n"
            "the SDK's own ``CredentialValue``; and test files.\n"
            "\n"
            "**Gated on the app's SDK.**  ``route_credentials`` first ships in\n"
            "application-sdk 3.40.0, so the check reads the version the app's\n"
            "``uv.lock`` resolves and stays silent unless it is at least that — a\n"
            "finding prescribing an import that does not exist yet would only teach\n"
            "people to suppress.  An app with no lock, or a lock that does not\n"
            "resolve the SDK, is silent too.\n"
            "\n"
            "Land as ``WARN``: every hit is a working copy awaiting migration.  A\n"
            "site the seam genuinely does not cover — ``CredentialRef.resolve`` over\n"
            "an object that is not the entry-point input — records that with a\n"
            "justified ``# conformance: ignore[P053] <reason>``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p053",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
)
