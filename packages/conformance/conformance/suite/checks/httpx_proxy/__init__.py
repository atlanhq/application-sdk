"""P-series httpx-proxy check — AST-based.

* ``P066`` HttpxTransportWithoutProxy (app) — an ``httpx.AsyncHTTPTransport`` /
  ``httpx.HTTPTransport`` built without ``proxy=``.  httpx skips the
  environment proxy for any client given an explicit transport, so the app's
  requests bypass ``HTTPS_PROXY`` on proxy-only hosts.

Registered under series letter ``P`` alongside ``client_seam`` and the other
seam checks, the established multi-module pattern.  Uses the shared discovery
walk, which excludes ``tests/`` dirs: a test transport never sees a customer
proxy.

Inline suppression
------------------
Add ``# conformance: ignore[P066] <reason>`` on the offending line (or the
comment-only line directly above it) — or, better, pass an explicit
``proxy=None`` when the transport only ever reaches localhost.
"""

from __future__ import annotations

import ast
import sys

from conformance.suite.checks._ast_common import (
    _parse_directives,
    discover,
    make_cli_main,
    make_scan_path,
)
from conformance.suite.schema.findings import Finding

from ._detect import check_p066

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for P066."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    return check_p066(tree, file, _parse_directives(text))


scan_path = make_scan_path(scan_text)


main = make_cli_main(
    scan_text,
    description=(
        "httpx-proxy P-series check (P066): scan Python files for httpx "
        "transports built without proxy=, which bypass HTTPS_PROXY."
    ),
)
"""CLI entry point for the httpx-proxy check."""


if __name__ == "__main__":
    sys.exit(main())
