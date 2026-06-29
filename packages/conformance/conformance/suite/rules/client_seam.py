"""Client-seam rule definitions (P019, BLDX-1430).

``pyatlan`` is a core SDK dependency and the single supported way to talk to an
Atlan service.  The SDK wraps it behind ``application_sdk.credentials`` —
``AtlanClientMixin.get_or_create_async_atlan_client()`` and
``create_async_atlan_client()`` — so apps reach Atlan through a typed, cached,
observability-stamped client instead of hand-rolling HTTP.

This rule guards against *reinventing* that client: raw ``httpx`` / ``requests`` /
``aiohttp`` / ``urllib`` access aimed at an Atlan service (Atlas, Heracles, …)
when ``pyatlan`` already covers it (BLDX-1430).

Why this rule is narrow and heuristic
--------------------------------------
``pyatlan`` hides endpoint routing, so there is **no** Atlan host/endpoint
constant in product code to key off.  The only static "this targets Atlan"
signal is the API path: ``/api/meta`` (Atlas metastore) and ``/api/service``
(Heracles).  We therefore flag a raw-HTTP request **only** when its URL argument
statically carries one of those markers.  This deliberately does *not* ban
``httpx``/``requests`` wholesale — the SDK and apps make heavy, legitimate
non-Atlan HTTP calls (Dapr sidecar, Segment, Prometheus, Temporal metrics,
third-party connector APIs, proxy detection).  A URL built entirely from runtime
variables is an accepted false-negative; this is a ``WARN`` heuristic (cf. P012).

Constructing ``pyatlan``'s ``AtlanClient`` / ``AsyncAtlanClient`` directly is
**not** flagged: that is *reuse* of the blessed dependency, which is exactly what
the ticket asks for.

These are P-series (prescription) rules but live in their own module, modelled on
the orchestration-seam (P004–P007) and storage-seam (P008–P012) series.  P-ids
are a permanent public contract (see ``prescriptions.py``).

Scope
-----
``P019`` is ``both``: consumer apps are the primary target, but the SDK's own
product code has zero raw-Atlan-HTTP today, so running it on the SDK keeps that
clean and catches a future regression.  The SDK's one intentional raw-urllib
Atlan path is the e2e harness in ``application_sdk/testing/e2e/client.py``; it is
scanned (the ``testing`` subpackage is shipped, not a ``tests/`` dir) but stays
silent because it builds its request URL from variables
(``f"{self.tenant_url}{path}"``) — the documented heuristic false-negative — and
any deliberate exception is handled by inline ``# conformance: ignore[P019]``.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P019",
        scope=RuleScope.BOTH,
        name="RawHttpToAtlan",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="client-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.7.0",
        rationale=(
            "pyatlan is a core dependency and the single supported client for Atlan "
            "services; the SDK exposes it via application_sdk.credentials "
            "(AtlanClientMixin / create_async_atlan_client). Hand-rolling raw HTTP "
            "(httpx/requests/aiohttp/urllib) against an Atlan endpoint re-implements "
            "auth, retries, pagination, and typing that pyatlan already provides, and "
            "drifts from the contract every other app follows (BLDX-1430)."
        ),
        short_description=(
            "Raw HTTP request to an Atlan endpoint (/api/meta, /api/service) "
            "instead of the pyatlan client"
        ),
        full_description=(
            "A raw HTTP call — ``httpx``/``requests``/``aiohttp`` request method or\n"
            "``urllib.request.urlopen``/``Request`` — targets an Atlan service: its\n"
            "URL statically contains ``/api/meta`` (Atlas metastore) or\n"
            "``/api/service`` (Heracles).  ``pyatlan`` is the supported client for\n"
            "these and is a core SDK dependency; the SDK wraps it behind\n"
            "``application_sdk.credentials`` —\n"
            "``AtlanClientMixin.get_or_create_async_atlan_client()`` for App\n"
            "subclasses, or ``create_async_atlan_client(cred)`` for ad-hoc use — so\n"
            "callers get auth, retries, pagination, typed models, and the\n"
            "``x-atlan-app-*`` observability headers for free.  Hand-rolling HTTP\n"
            "reinvents all of that and diverges from the fleet-wide contract — see\n"
            "BLDX-1430.\n"
            "\n"
            "Constructing ``pyatlan``'s ``AtlanClient`` / ``AsyncAtlanClient`` directly\n"
            "is **not** flagged — that is reuse of the blessed dependency.\n"
            "\n"
            "This rule is a **heuristic**: the request destination is a runtime value\n"
            "and pyatlan hides routing, so the only static Atlan signal is the URL\n"
            "path marker.  It matches the marker against the call's URL argument\n"
            "(string literal, f-string literal segments, or ``+`` concatenation); a\n"
            "URL assembled entirely from variables will not be caught.  Legitimate\n"
            "non-Atlan HTTP (Dapr ``/v1.0``, Segment, Prometheus, Temporal metrics,\n"
            "third-party connector APIs, proxy detection, ``urllib.parse`` helpers)\n"
            "carries no such marker and stays silent.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P019]\n"
            "<reason>`` records any unavoidable exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p019",
    ),
)
