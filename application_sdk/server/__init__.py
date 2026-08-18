"""HTTP server layer — FastAPI app, MCP integration, middleware.

Import from the submodules (``application_sdk.server.middleware``,
``application_sdk.server.mcp``); nothing is re-exported here, and nothing is
imported at package scope so that reaching one submodule never pulls in the
others' dependencies.

This file is intentionally almost empty. It exists because a package without an
``__init__.py`` is an implicit namespace package, which static tools — griffe,
and therefore the capability manifest — do not descend into: the public surface
of ``server.mcp`` and ``server.middleware`` was invisible to every static reader
of this package while it was missing (FND-439). The previous ``__init__.py`` was
deleted wholesale with the v2 shim layer (b5ed0e49) because it held the
deprecated ``ServerInterface``; only the deprecated content was meant to go.
"""
