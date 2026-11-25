"""v2 → v3 migration tooling for application-sdk connectors.

Holds the class-name tables every module of the tool reads, so there is one
copy of each. Stdlib only: ``check_migration`` imports this package and must
run without the SDK installed.
"""

from __future__ import annotations

#: The SDK's ``App``-family bases: ``App`` plus ``application_sdk.templates.__all__``.
#: A class extending one of these is a v3 app. Equal to conformance's
#: ``SDK_APP_BASE_NAMES`` (``conformance/suite/checks/_ast_common/_sdk_app.py``);
#: ``tests/unit/tools/test_import_mapping.py`` fails if the two drift apart.
SDK_APP_BASE_NAMES: frozenset[str] = frozenset(
    {
        "App",
        "SqlApp",
        "BaseMetadataExtractor",
        "IncrementalSqlMetadataExtractor",
        "SqlMetadataExtractor",
        "SqlQueryExtractor",
    }
)

#: Handler base classes, v2 and v3. A class extending one of these is a handler.
HANDLER_BASE_NAMES: frozenset[str] = frozenset(
    {"BaseHandler", "Handler", "DefaultHandler", "BaseSQLHandler", "HandlerInterface"}
)
