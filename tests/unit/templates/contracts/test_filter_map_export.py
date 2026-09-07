"""FilterMap is re-exported from application_sdk.templates.contracts.

P001's catalog points builders at that import path; a missing re-export is
an ImportError on the documented remediation.
"""

from __future__ import annotations

from application_sdk.templates.contracts import FilterMap
from application_sdk.templates.contracts.sql_metadata import FilterMap as SqlFilterMap


def test_filter_map_is_reexported_from_templates_contracts() -> None:
    assert FilterMap is SqlFilterMap
