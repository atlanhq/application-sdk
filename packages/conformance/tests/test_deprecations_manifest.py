"""Drift guard for the deprecated-symbol manifest — the forcing function for B001.

The committed ``conformance/data/deprecated_symbols.json`` is what lets B001 fan
out fleet-wide with no per-app work: consumer apps read it, never the SDK source.
For that to mean anything the committed file must always equal a fresh scan of
``application_sdk/`` — otherwise an app could be told a symbol is fine when the
SDK has just deprecated it.

This test makes a new ``@deprecated`` (or a class that begins warning on
construction) fail CI until the manifest is regenerated in the same PR:

    uv run atlan-application-sdk-conformance gen-deprecations

It is the manifest counterpart of ``assert_registry_consistent`` /
``test_catalog_*`` for the rule catalog.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks.deprecation._manifest import (
    MANIFEST_PATH,
    SDK_IMPORT_ROOT,
    build_manifest,
    load_manifest,
    serialize,
)


def _find_sdk_root() -> Path | None:
    """Locate the repo root containing ``application_sdk/`` from this test file."""
    for parent in Path(__file__).resolve().parents:
        if (parent / SDK_IMPORT_ROOT / "__init__.py").is_file():
            return parent
    return None


def test_manifest_is_committed() -> None:
    """The manifest file exists and parses."""
    assert MANIFEST_PATH.is_file(), (
        f"{MANIFEST_PATH} is missing — run "
        "`uv run atlan-application-sdk-conformance gen-deprecations`."
    )
    manifest = load_manifest()
    assert manifest.symbols, "committed manifest has no symbols"


def test_manifest_contains_legacy_transformers() -> None:
    """BLDX-1399: the legacy transformer surface is recorded for B001.

    Stronger than the drift test (which only proves the file matches a fresh
    scan): this pins the *intent* — the three transformer symbols must stay
    marked, with a well-formed asset-mapper migration target and a v4.0 removal,
    so B001 keeps steering apps onto the asset-mapper pattern.  If a refactor
    drops a class-level marker, the drift test would still pass after a
    regenerate; this one fails loudly.
    """
    manifest = load_manifest()
    by_name = {s.symbol: s for s in manifest.symbols}
    expected = {
        "TransformerInterface": "application_sdk.transformers",
        "AtlasTransformer": "application_sdk.transformers.atlas",
        "QueryBasedTransformer": "application_sdk.transformers.query",
    }
    for symbol, module in expected.items():
        assert (
            symbol in by_name
        ), f"{symbol} missing from manifest — B001 is blind to it"
        rec = by_name[symbol]
        assert rec.module == module
        assert rec.kind == "class"
        assert rec.migration_target, f"{symbol} notice names no migration target (B002)"
        assert rec.removal_version == "4.0", f"{symbol} removal version drifted"
        assert "asset-mapper" in rec.message


def test_committed_manifest_matches_fresh_scan() -> None:
    """The committed manifest equals a fresh scan of application_sdk/.

    If this fails, the SDK's deprecation surface changed but the manifest was not
    regenerated.  Run ``gen-deprecations`` and commit the result in the same PR.
    """
    sdk_root = _find_sdk_root()
    if sdk_root is None:
        pytest.skip("application_sdk/ not found alongside the conformance package")

    fresh = build_manifest(sdk_root)
    on_disk = MANIFEST_PATH.read_text(encoding="utf-8")
    assert on_disk == serialize(fresh), (
        "deprecated_symbols.json is stale — regenerate with "
        "`uv run atlan-application-sdk-conformance gen-deprecations` and commit it."
    )
