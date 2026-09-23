"""The SDK ``App``-family base classes that consumer code may subclass.

``App`` itself (``application_sdk.app``) plus every public base in
``application_sdk.templates``. Each one is an ``App`` subclass, so a consumer
class deriving from any of them gets the same ``run`` → ``@workflow.run``,
``@entrypoint`` → workflow and ``@task`` → activity wiring. Checks that anchor
on "an ``App`` subclass" must accept the whole family, or a connector built on
``SqlApp`` is invisible to them.

``tests/test_app_name_alignment.py::test_sdk_base_names_matches_templates_all``
keeps this set equal to ``application_sdk.templates.__all__ | {"App"}``.
"""

from __future__ import annotations

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
