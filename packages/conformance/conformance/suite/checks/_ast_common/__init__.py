"""Shared helpers for AST-based check series (E, P, O, …).

This is the neutral home for the infrastructure every AST series needs: the
Python-source discovery walk, the ``# conformance: ignore[...]`` directive
parser, suppression-aware ``Finding`` construction, and the per-series CLI
skeleton.  It deliberately depends on no single series, so the E/P/O series all
consume it without reaching into one another's private surface.
"""

from __future__ import annotations

from ._cli import TOOL_VERSION, make_cli_main
from ._directives import _IgnoreDirective, _parse_directives, parse_ignore_directive
from ._discovery import EXCLUDE_DIRS, discover
from ._exc_info import has_exc_info_traceback
from ._findings import make_finding
from ._imports import collect_import_origins, qualify_chained_attr_call
from ._io import safe_read_json, safe_read_text
from ._pytest_collection import (
    is_collectable_test_file,
    is_test_class,
    is_test_function,
)
from ._scope import SDK_PACKAGE_PREFIX, detect_scope, is_sdk_package_name
from ._toml_suppress import (
    SuppressionsMap,
    _is_suppressed,
    make_toml_finding,
    parse_toml_suppressions,
)

__all__ = [
    "safe_read_json",
    "safe_read_text",
    "EXCLUDE_DIRS",
    "SDK_PACKAGE_PREFIX",
    "TOOL_VERSION",
    "SuppressionsMap",
    "_IgnoreDirective",
    "_is_suppressed",
    "_parse_directives",
    "collect_import_origins",
    "detect_scope",
    "has_exc_info_traceback",
    "discover",
    "is_collectable_test_file",
    "is_sdk_package_name",
    "is_test_class",
    "is_test_function",
    "make_cli_main",
    "make_finding",
    "make_toml_finding",
    "parse_ignore_directive",
    "parse_toml_suppressions",
    "qualify_chained_attr_call",
]
