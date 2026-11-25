"""Tests for tools/migrate_v3/import_mapping.py and the package's shared name tables."""

from __future__ import annotations

import re
from pathlib import Path

import orjson

from tools.migrate_v3 import SDK_APP_BASE_NAMES
from tools.migrate_v3.import_mapping import (
    DEPRECATED_MODULE_ROOTS,
    MODULE_MAP,
    SYMBOL_MAP,
)

_MANIFEST = (
    Path(__file__).resolve().parents[3]
    / "packages/conformance/conformance/data/deprecated_symbols.json"
)

#: Deprecated targets the rewriter still writes, each with the reason.
#: SqlQueryExtractor: the owner review of #4552 (FND-4313) kept query-log
#: connectors on it until SqlApp has an opt-in query-log capability; the
#: upgrade guide (Step 2) and MIGRATION_PROMPT.md §2b teach it until then.
#: When that capability lands, retarget the three entries and drop this.
#: TaskResult (FND-4914): v2's ActivityResult has no non-deprecated equivalent;
#: the entry is structural, so its TODO tells the app to replace it. Retarget
#: or drop the entry when TaskResult is removed in v4.0.
_KNOWN_DEPRECATED_TARGETS = frozenset(
    {
        ("application_sdk.templates", "SqlQueryExtractor"),
        ("application_sdk.common.models", "TaskResult"),
    }
)


def _deprecated_symbols() -> list[dict[str, object]]:
    return orjson.loads(_MANIFEST.read_bytes())["symbols"]


def _is_deprecated(module: str, symbol: str) -> bool:
    """True if ``from <module> import <symbol>`` reaches a deprecated name.

    The manifest records the defining module; a target may import it through
    a parent package's re-export (``application_sdk.templates`` re-exports
    ``application_sdk.templates.sql_metadata_extractor.SqlMetadataExtractor``).
    Methods and properties are not importable, so they never match.
    """
    for entry in _deprecated_symbols():
        if entry["symbol"] != symbol or entry["kind"] in ("method", "property"):
            continue
        defining = str(entry["module"])
        if defining == module or defining.startswith(module + "."):
            return True
    return False


def test_no_symbol_target_is_deprecated() -> None:
    """The rewriter must not move an app onto a name the SDK tells it to leave
    (conformance B001)."""
    deprecated = {
        (t.new_module, t.new_symbol)
        for t in SYMBOL_MAP.values()
        if t.new_symbol is not None and _is_deprecated(t.new_module, t.new_symbol)
    }
    assert deprecated == _KNOWN_DEPRECATED_TARGETS


def test_the_deprecation_lookup_sees_re_exported_templates() -> None:
    """Guards the guard: through the ``templates`` re-export, the deprecated
    template the six SQL metadata entries used to target is found."""
    assert _is_deprecated("application_sdk.templates", "SqlMetadataExtractor")
    assert not _is_deprecated("application_sdk.templates", "SqlApp")


def test_no_note_recommends_a_deprecated_template() -> None:
    """A TODO note naming a deprecated template sends the reader back to it."""
    templates = {
        str(e["symbol"])
        for e in _deprecated_symbols()
        if str(e["module"]).startswith("application_sdk.templates.")
        and e["kind"] == "class"
    }
    allowed = {symbol for _, symbol in _KNOWN_DEPRECATED_TARGETS}
    notes = [t.note for t in SYMBOL_MAP.values()] + [
        n for _, _, n in MODULE_MAP.values()
    ]
    named = {
        name
        for note in notes
        for name in templates - allowed
        if re.search(rf"\b{name}\b", note)
    }
    assert named == set()


def test_every_mapped_module_is_under_a_deprecated_root() -> None:
    """check_migration fails on DEPRECATED_MODULE_ROOTS; a v2 module the
    rewriter maps but the checker would pass is a gap between the two."""
    sources = set(MODULE_MAP) | {module for module, _ in SYMBOL_MAP}
    uncovered = {
        m
        for m in sources
        if not any(m == r or m.startswith(r + ".") for r in DEPRECATED_MODULE_ROOTS)
    }
    assert uncovered == set()


def test_sdk_app_base_names_match_conformance_and_the_sdk() -> None:
    """One table for the tool, held equal to conformance's copy and to
    ``App`` plus ``application_sdk.templates.__all__``."""
    from conformance.suite.checks._ast_common._sdk_app import (
        SDK_APP_BASE_NAMES as CONFORMANCE_SDK_APP_BASE_NAMES,
    )

    import application_sdk.templates as templates

    assert SDK_APP_BASE_NAMES == CONFORMANCE_SDK_APP_BASE_NAMES
    assert SDK_APP_BASE_NAMES == frozenset(templates.__all__) | {"App"}
