"""Application-layer gateway auth headers.

Some deployments route all Atlan-bound egress through a customer-managed API
gateway that authenticates *every request itself* (custom headers, Basic, or a
signed JWT) rather than accepting standard HTTP forward-proxy env vars. This
module surfaces those headers from a single env var (``ATLAN_GATEWAY_AUTH_HEADERS``)
so they can be injected on the HTTP egress paths that can carry an
application-layer header:

* the OAuth token exchange (``credentials/oauth.py``), and
* the event binding (``execution/_temporal/interceptors/events.py``).

Egress whose wire auth cannot carry an extra header is intentionally **not**
covered here:

* object-store upload — SigV4 signs the request; an added/rewritten header
  breaks the signature. Route via TCP/TLS pass-through (obstore ``proxy_url``).
* Temporal — gRPC/TLS; use mTLS at the edge (Temporal ``TLSConfig`` client cert
  / ``HttpConnectProxyConfig``).

Gated: returns ``{}`` unless ``ATLAN_GATEWAY_AUTH_HEADERS`` is set to a JSON
object, so non-gateway deployments are completely unaffected.
"""

from __future__ import annotations

import json

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def gateway_auth_headers() -> dict[str, str]:
    """Return the configured application-layer gateway auth headers.

    Reads ``ATLAN_GATEWAY_AUTH_HEADERS`` (a JSON object of ``{header: value}``)
    at call time. Returns an empty dict when unset, empty, not valid JSON, or
    not a JSON object — a misconfiguration never raises and never partially
    injects; it degrades to "no gateway headers" and logs a warning.

    Values are stringified. Header names are used verbatim.
    """
    # Read via the module attribute (not a cached import) so the value tracks
    # the process env and stays overridable in tests.
    from application_sdk import (  # noqa: PLC0415 — avoid import cycle at module load
        constants,
    )

    raw = constants.GATEWAY_AUTH_HEADERS
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "ATLAN_GATEWAY_AUTH_HEADERS is not valid JSON — ignoring; no gateway "
            "auth headers will be injected"
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "ATLAN_GATEWAY_AUTH_HEADERS must be a JSON object of {header: value} "
            "— ignoring"
        )
        return {}
    return {str(k): str(v) for k, v in parsed.items()}
