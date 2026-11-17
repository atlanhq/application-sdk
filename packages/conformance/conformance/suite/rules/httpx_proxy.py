"""httpx-proxy rule definition (P066).

httpx honors ``HTTP(S)_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY`` only when a client
builds its own transport (``allow_env_proxies = trust_env and transport is
None`` in ``httpx/_client.py``).  Apps build ``httpx.AsyncHTTPTransport(...)``
to carry ``verify=`` (httpx ignores a client-level ``verify=`` next to a
transport), pool ``limits=`` or connect ``retries=`` — and in doing so silently
drop the environment proxy.  A production RCA hit exactly this: on a
self-deployed runtime whose egress was proxy-only, a connector's sign-in timed
out with no HTTP response while a plain ``httpx.AsyncClient()`` in the same
container reached the source through the proxy.

* ``P066`` HttpxTransportWithoutProxy — an ``httpx.AsyncHTTPTransport`` /
  ``httpx.HTTPTransport`` constructed without a ``proxy=`` keyword.

Scope
-----
``app``: the SDK's own ``BaseClient`` default transport applies the
environment proxy itself, and its remaining transports (the Dapr sidecar
client) only reach localhost.  Consumer apps are where hand-built transports
reach customer sources.

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

_CLIENTS_DOC = (
    "https://github.com/atlanhq/application-sdk/blob/main/docs/concepts/clients.md"
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P066",
        canonical_reference=(
            "atlan-openapi-app app/api_client.py — builds its client as "
            "`httpx.AsyncClient(...)` without `transport=`, so httpx applies "
            "HTTPS_PROXY / NO_PROXY itself. Apps that need a custom transport "
            "subclass the SDK's BaseClient, whose default transport resolves the "
            "environment proxy per request (" + _CLIENTS_DOC + ")."
        ),
        scope=RuleScope.APP,
        name="HttpxTransportWithoutProxy",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="client-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.59.0",
        rationale=(
            "httpx skips HTTPS_PROXY / NO_PROXY for any client given an explicit "
            "transport. An httpx transport built without proxy= therefore makes "
            "every request through it connect directly; on a self-deployed runtime "
            "whose egress is proxy-only, those requests time out with no HTTP "
            "response and the source looks unreachable."
        ),
        short_description=(
            "httpx.AsyncHTTPTransport / HTTPTransport built without `proxy=` "
            "(bypasses HTTPS_PROXY / NO_PROXY)"
        ),
        full_description=(
            "A call constructs ``httpx.AsyncHTTPTransport(...)`` or\n"
            "``httpx.HTTPTransport(...)`` with no ``proxy=`` keyword.  httpx reads\n"
            "the proxy environment variables only when the client builds its own\n"
            "transport — ``allow_env_proxies = trust_env and transport is None`` —\n"
            "so a client given this transport ignores ``HTTPS_PROXY``,\n"
            "``HTTP_PROXY``, ``ALL_PROXY`` and ``NO_PROXY``.  Apps usually build a\n"
            "transport to set ``verify=`` (a client-level ``verify=`` is ignored\n"
            "next to a transport), pool ``limits=`` or connect ``retries=``, and\n"
            "lose the proxy as a side effect.  On a host whose egress is\n"
            "proxy-only the request then connects directly and times out.\n"
            "\n"
            "Fix — resolve the proxy for the request URL and pass it on::\n"
            "\n"
            "    import urllib.request\n"
            "\n"
            "    def env_proxy_for(url: str) -> str | None:\n"
            "        parsed = httpx.URL(url)\n"
            "        proxies = urllib.request.getproxies_environment()\n"
            '        proxy = proxies.get(parsed.scheme) or proxies.get("all")\n'
            "        if not proxy or urllib.request.proxy_bypass_environment(\n"
            "            parsed.host, proxies\n"
            "        ):\n"
            "            return None\n"
            "        return proxy\n"
            "\n"
            "    transport = httpx.AsyncHTTPTransport(\n"
            "        verify=ctx, limits=limits, proxy=env_proxy_for(url)\n"
            "    )\n"
            "\n"
            "or drop the custom transport and let the client build one\n"
            "(``httpx.AsyncClient(verify=ctx, limits=limits)``), or subclass the\n"
            "SDK's ``BaseClient``, whose default transport applies the environment\n"
            "proxy itself.  See " + _CLIENTS_DOC + ".\n"
            "\n"
            "Any explicit ``proxy=`` satisfies the rule — pass ``proxy=None`` for a\n"
            "transport that only ever reaches localhost (a sidecar, a local\n"
            "emulator).  A call that splats ``**kwargs`` is not flagged, since the\n"
            "proxy may be among them.  Subclasses of the transport and\n"
            "``httpx.MockTransport`` are not matched.  Test directories are not\n"
            "scanned.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P066]\n"
            "<reason>`` records a deliberate exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p066",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
)
