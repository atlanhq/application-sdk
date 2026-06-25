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
from ._findings import make_finding
from ._scope import SDK_PACKAGE_PREFIX, detect_scope, is_sdk_package_name

__all__ = [
    "EXCLUDE_DIRS",
    "SDK_PACKAGE_PREFIX",
    "TOOL_VERSION",
    "_IgnoreDirective",
    "_parse_directives",
    "detect_scope",
    "discover",
    "is_sdk_package_name",
    "make_cli_main",
    "make_finding",
    "parse_ignore_directive",
]
