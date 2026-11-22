"""Drift guard for the public-error allowlist — the forcing function for P043/P045.

The committed ``conformance/data/public_errors.json`` is what lets the error-seam
rules name the right remediation offline inside a consumer app repo, which never
has the SDK's ``application_sdk/errors/__init__.py`` on disk.  For that to stay
true the committed file must equal a fresh read of the SDK's public error surface.

This test makes a change to ``application_sdk.errors.__all__`` fail CI until the
allowlist is regenerated in the same PR:

    uv run atlan-application-sdk-conformance gen-public-errors
"""

from __future__ import annotations

from conformance.suite.checks.error_seam._public_error_surface import (
    ALLOWLIST_PATH,
    build_allowlist,
    load_allowlist,
    serialize,
)
from tests._helpers import sdk_root


def test_committed_allowlist_matches_sdk_source() -> None:
    expected = serialize(build_allowlist(sdk_root()))

    assert ALLOWLIST_PATH.read_text(encoding="utf-8") == expected, (
        "public_errors.json is stale — run "
        "`uv run atlan-application-sdk-conformance gen-public-errors`."
    )


def test_allowlist_loads_and_holds_the_public_error_base() -> None:
    names = load_allowlist()

    assert "AppError" in names


def test_allowlist_holds_the_promoted_object_store_classes() -> None:
    """Proves the promotion landed before the rules that reference it."""
    names = load_allowlist()

    assert "ObjectStoreReadError" in names
    assert "ObjectStoreDownloadError" in names


def test_allowlist_excludes_classes_that_stay_internal() -> None:
    names = load_allowlist()

    assert "FormatReadError" not in names
    assert "ReplacePrefixEmptyError" not in names
