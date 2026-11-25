"""The one rule for resolving outbound proxies from the environment.

Every SDK connection that can go through an egress proxy resolves it here: the
``BaseClient`` HTTP transport, the obstore object-store clients, the Temporal
gRPC connection and the S3 bucket-region lookup. Before this module each used
a slightly different rule, so on a proxy-only egress host some connections
worked while others timed out, depending on which variable was set.

The rule:

1. The proxy for a URL scheme is the scheme's variable (``HTTPS_PROXY`` for
   ``https``, ``HTTP_PROXY`` for ``http``), then ``ALL_PROXY``. There is no
   fallback from ``https`` to ``HTTP_PROXY``.
2. A host that matches ``NO_PROXY`` connects directly. Matching is the
   stdlib's: exact host, domain suffix (``.example.com`` and ``example.com``
   both match subdomains), or ``*`` for every host; a port on the host is
   ignored.
3. Variables are read from the process environment only. The lowercase
   spelling wins over the uppercase one, as in the stdlib. Operating-system
   proxy settings (macOS System Settings, the Windows registry) are never
   read, so a developer laptop resolves the same way a pod does.

Stdlib-only: this module is in the API host's core install.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Mapping


def environment_proxies() -> dict[str, str]:
    """Return a snapshot of the proxy variables in the process environment.

    Keys are the lowercase scheme names (``"https"``, ``"http"``, ``"all"``)
    plus ``"no"`` for ``NO_PROXY``. Pass the snapshot to :func:`proxy_for`
    to resolve many URLs against one reading of the environment.
    """
    return urllib.request.getproxies_environment()


def proxy_for(
    scheme: str,
    host: str | None = None,
    *,
    proxies: Mapping[str, str] | None = None,
) -> str | None:
    """Return the proxy URL for a connection, or ``None`` to connect directly.

    Args:
        scheme: URL scheme of the connection, e.g. ``"https"``.
        host: Target host (a ``:port`` suffix is allowed). When given, a
            ``NO_PROXY`` match returns ``None``. Leave it out for a client
            that applies ``NO_PROXY`` itself (see :func:`no_proxy`).
        proxies: A snapshot from :func:`environment_proxies`. Defaults to
            reading the environment now.
    """
    if proxies is None:
        proxies = environment_proxies()
    proxy = proxies.get(scheme.lower()) or proxies.get("all")
    if not proxy:
        return None
    if host and urllib.request.proxy_bypass_environment(  # pyright: ignore[reportAttributeAccessIssue] — missing from typeshed
        host, dict(proxies)
    ):
        return None
    return proxy


def no_proxy(*, proxies: Mapping[str, str] | None = None) -> str | None:
    """Return the raw ``NO_PROXY`` list, or ``None`` when it is unset or empty.

    For clients that match ``NO_PROXY`` per request themselves (obstore's
    ``proxy_excludes``) and so cannot be given one resolved answer.
    """
    if proxies is None:
        proxies = environment_proxies()
    return proxies.get("no") or None
