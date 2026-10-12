"""Client-lifecycle rule definitions (P056–P057, FND-3778).

``BaseClient`` (``application_sdk.clients.base``) keeps one shared
``httpx.AsyncClient`` — and with it a connection pool of up to 10 idle
connections, 30 s expiry — open until ``close()`` is called.  It used to build a
client per request, so the pool was dropped after every call and ``close()``
never mattered; many apps therefore never call it.  Building a client per request
also meant one request's close tore down the shared transport under another
in-flight request, which a production crawl hit as a ``ReadError`` that silently
dropped models (application-sdk#4309, which made the client shared).

Since then an unclosed client holds its pool until the process exits.  The SDK
emits a ``ResourceWarning`` from ``BaseClient.__del__`` when a client that made
requests was never closed, but Python's default filters hide ``ResourceWarning``
outside pytest / ``-X dev``, so production never sees it.  These rules carry the
same signal statically, through each app's own conformance scan:

* ``P056`` UnclosedBaseClient — a function binds a ``BaseClient`` subclass
  instance to a local name and neither enters it with ``async with`` nor calls
  ``await <name>.close()`` in a ``finally``.
* ``P057`` CloseOverrideSkipsSuper — a ``BaseClient`` subclass overrides
  ``close()`` and never calls ``super().close()``, so the shared pool is never
  released even by callers that close correctly.

Why the rules are narrow and heuristic
--------------------------------------
Ownership of a client is a runtime fact.  ``P056`` only reasons within a single
function: a client that is returned, yielded, stored on an attribute or in a
container, or passed as an argument has moved to another owner and is an
accepted false negative — tracking it across functions would trade a WARN's
precision for a guess.  A module-level client is a process-lifetime singleton
and is out of scope.  A ``close()`` reached only on the happy path (not in a
``finally``) still fires: the exception path is exactly when it leaks.

Subclass resolution is name-keyed across the repo and transitive, seeded only
from a ``BaseClient`` imported from ``application_sdk.clients`` /
``application_sdk.clients.base`` — an unrelated ``BaseClient`` from another
library never seeds it.  A factory that returns a client (``build_client(...)``)
is not a constructor call and is not followed.

SDK-version gate
----------------
On an SDK that predates the shared client the pool is dropped per request, so
an unclosed client leaks nothing and ``async with`` is not available.  The
check is silent unless the app's ``uv.lock`` resolves ``atlan-application-sdk``
at or above the floor in ``suite.checks.client_lifecycle``.

Scope
-----
Both rules are ``app``-scoped: the SDK defines ``BaseClient`` but constructs no
subclass of it in product code, and the version gate reads a *consumed* SDK
version, which the SDK repo does not have.

These are P-series (prescription) rules in their own module, modelled on the
client seam (P019).  P-ids are a permanent public contract (see
``prescriptions.py``).
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

_CLIENTS_DOC = (
    "https://github.com/atlanhq/application-sdk/blob/main/docs/concepts/clients.md"
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P056",
        canonical_reference=(
            "atlan-metabase-app app/handler.py — `test_auth` builds its client with "
            "`client = await build_client(credential)` and closes it in "
            "`try: ... finally: await client.close()`; `fetch_metadata` and "
            "`preflight_check` close the client they own in a `finally` guarded by "
            "`if owns_client:`. The SDK's own pattern is "
            "`async with MyClient() as client:` (" + _CLIENTS_DOC + ")."
        ),
        scope=RuleScope.APP,
        name="UnclosedBaseClient",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="client-lifecycle",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.47.0",
        rationale=(
            "BaseClient keeps one shared httpx connection pool open until close() is "
            "called. A client that is never closed — or closed only on the happy "
            "path — holds its idle connections for the life of the worker process, "
            "and the ResourceWarning the SDK raises for it is hidden by Python's "
            "default warning filters in production (FND-3778)."
        ),
        short_description=(
            "BaseClient subclass constructed in a function and never closed "
            "(no `async with`, no `close()` in a `finally`)"
        ),
        full_description=(
            "A function binds an instance of a ``BaseClient`` subclass to a local\n"
            "name (``client = MyClient(...)``) and neither enters it with\n"
            "``async with client`` nor calls ``await client.close()`` inside a\n"
            "``finally`` in the same function.  ``BaseClient`` keeps one shared\n"
            "``httpx.AsyncClient`` and its connection pool open until ``close()``,\n"
            "so the client's idle connections stay open for the life of the worker.\n"
            "A ``close()`` outside a ``finally`` is skipped by any exception raised\n"
            "before it, which is when the leak happens.\n"
            "\n"
            "Fix — prefer ``async with``, which closes on exit, including on an\n"
            "exception (``__aenter__`` returns the client and does not call\n"
            "``load()``)::\n"
            "\n"
            "    async with MyClient() as client:\n"
            "        await client.load(credentials=creds)\n"
            "        ...\n"
            "\n"
            "or close in a ``finally``::\n"
            "\n"
            "    client = MyClient()\n"
            "    try:\n"
            "        ...\n"
            "    finally:\n"
            "        await client.close()\n"
            "\n"
            "See " + _CLIENTS_DOC + ".\n"
            "\n"
            "This is a **heuristic** that reasons within one function.  A client\n"
            "that is returned, yielded, stored on an attribute or container, or\n"
            "passed as an argument has moved to another owner and is not flagged;\n"
            "neither is a module-level client.  Subclasses are resolved by name\n"
            "across the repo, transitively, from a ``BaseClient`` imported from\n"
            "``application_sdk.clients``.  The check is silent unless the app's\n"
            "``uv.lock`` resolves an SDK whose ``BaseClient`` shares its pool.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P056]\n"
            "<reason>`` records a deliberate exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p056",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
    RuleDefinition(
        id="P057",
        canonical_reference=(
            "application_sdk application_sdk/clients/base.py — `BaseClient.close()` "
            "closes the shared `httpx.AsyncClient` and marks the client closed; a "
            "subclass `close()` does its own cleanup and then "
            "`await super().close()`, as documented in docs/concepts/clients.md. No "
            "reference app overrides close() on a BaseClient subclass correctly: "
            "atlan-metabase-app's `MetabaseApiClient.close()` is the shape this rule "
            "flags."
        ),
        scope=RuleScope.APP,
        name="CloseOverrideSkipsSuper",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="client-lifecycle",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.47.0",
        rule_interactions=(
            "P056 (UnclosedBaseClient) checks that callers close the client; this "
            "rule checks that closing it actually releases the pool. A caller that "
            "closes correctly in a `finally` still leaks if the subclass's close() "
            "skips super().close(), so clearing P056 does not clear this."
        ),
        rationale=(
            "BaseClient.close() releases the shared httpx connection pool. A "
            "subclass that overrides close() without calling super().close() keeps "
            "that pool open even when every caller closes the client correctly "
            "(FND-3778)."
        ),
        short_description=(
            "`close()` override on a BaseClient subclass never calls "
            "`super().close()`"
        ),
        full_description=(
            "A ``BaseClient`` subclass defines ``close()`` and its body never calls\n"
            "``super().close()``.  ``BaseClient.close()`` releases the shared\n"
            "``httpx.AsyncClient`` and its connection pool; an override that skips\n"
            "it (for example, one that only clears a session token) leaves the pool\n"
            "open even when callers close the client in a ``finally`` or with\n"
            "``async with``.\n"
            "\n"
            "Fix — keep the subclass cleanup and add the super call::\n"
            "\n"
            "    async def close(self) -> None:\n"
            "        self.session_token = None\n"
            "        await super().close()\n"
            "\n"
            "See " + _CLIENTS_DOC + ".\n"
            "\n"
            "Subclasses are resolved by name across the repo, transitively, from a\n"
            "``BaseClient`` imported from ``application_sdk.clients``; an override\n"
            "on an intermediate subclass counts.  The check is silent unless the\n"
            "app's ``uv.lock`` resolves an SDK whose ``BaseClient`` shares its pool.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P057]\n"
            "<reason>`` records a deliberate exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p057",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
)
