"""Property-based tests for object-store path normalization.

``normalize_key`` is the single v2 → v3 key-mapping function: every public
storage helper funnels keys through it.  These properties pin the invariants
app developers rely on for the documented input shapes (clean store keys and
``TEMPORARY_PATH``-staged workflow paths):

* normalization is idempotent
* a staging-prefixed key and its bare form normalize to the same store key
* normalized keys are clean (no leading/trailing slash, no backslashes,
  no ``..`` segments for traversal-free inputs)

``_safe_join_under`` is the download-side traversal guard; its property is
that no input — including hostile ``..``-laden keys — can ever produce a
path outside the destination root.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from application_sdk import constants
from application_sdk.storage.errors import StorageError
from application_sdk.storage.ops import _safe_join_under, normalize_key

# The staging root contains "." which is outside the segment alphabet below,
# so no generated key can collide with (or embed) the staging path itself.
_STAGING_ROOT = "/tmp/atlan-sdk.property-staging"

# POSIX-safe key segments: no ".", so "." / ".." segments cannot be generated.
_segment = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
    min_size=1,
    max_size=12,
)

# Clean relative store keys: "a/b/c"-shaped, the documented v3 input.
_clean_key = st.lists(_segment, min_size=1, max_size=6).map("/".join)

# Key-ish inputs in every documented spelling of the same logical key.
_key_spellings = st.sampled_from(
    [
        lambda k: k,  # bare v3 store key
        lambda k: f"{_STAGING_ROOT}/{k}",  # v2 staging path
        lambda k: f"/{k}",  # absolute path
        lambda k: f"{k}/",  # trailing slash
        lambda k: k.replace("/", "\\"),  # Windows separators
    ]
)


@given(key=_clean_key, spelling=_key_spellings)
@settings(max_examples=100)
def test_normalize_key_is_idempotent(key: str, spelling) -> None:
    """normalize(normalize(k)) == normalize(k) for every documented spelling."""
    with mock.patch.object(constants, "TEMPORARY_PATH", _STAGING_ROOT):
        once = normalize_key(spelling(key))
        twice = normalize_key(once)
    assert twice == once


@given(key=_clean_key)
@settings(max_examples=100)
def test_staging_prefixed_and_bare_keys_normalize_identically(key: str) -> None:
    """The v2 staging spelling and the v3 bare key map to the same store key.

    This is the core v2 → v3 migration contract: both input shapes must be
    interchangeable through the public storage API.
    """
    with mock.patch.object(constants, "TEMPORARY_PATH", _STAGING_ROOT):
        assert normalize_key(f"{_STAGING_ROOT}/{key}") == normalize_key(key) == key


@given(key=_clean_key, spelling=_key_spellings)
@settings(max_examples=100)
def test_normalized_keys_are_clean(key: str, spelling) -> None:
    """Outputs never carry separator artifacts or traversal segments."""
    with mock.patch.object(constants, "TEMPORARY_PATH", _STAGING_ROOT):
        normalized = normalize_key(spelling(key))

    assert not normalized.startswith("/")
    assert not normalized.endswith("/")
    assert "\\" not in normalized
    assert ".." not in normalized.split("/")


@given(key=_clean_key)
@settings(max_examples=50)
def test_backslash_and_forward_slash_spellings_agree(key: str) -> None:
    with mock.patch.object(constants, "TEMPORARY_PATH", _STAGING_ROOT):
        assert normalize_key(key.replace("/", "\\")) == normalize_key(key)


def test_empty_key_normalizes_to_empty() -> None:
    assert normalize_key("") == ""


def test_staging_root_itself_normalizes_to_store_root() -> None:
    with mock.patch.object(constants, "TEMPORARY_PATH", _STAGING_ROOT):
        assert normalize_key(_STAGING_ROOT) == ""


# ---------------------------------------------------------------------------
# Traversal guard: _safe_join_under never escapes the destination root
# ---------------------------------------------------------------------------

# Hostile-ish relative paths: normal segments mixed with "..", ".", and
# leading slashes — the shapes a malicious or corrupted store listing could
# feed into the download path.
_traversal_rel = st.lists(
    st.one_of(_segment, st.just(".."), st.just(".")),
    min_size=1,
    max_size=8,
).map("/".join)


@given(rel=_traversal_rel, leading_slash=st.booleans())
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_safe_join_under_never_escapes_root(
    tmp_path: Path, rel: str, leading_slash: bool
) -> None:
    """Every input either raises StorageError or resolves inside the root."""
    candidate = f"/{rel}" if leading_slash else rel
    root = tmp_path.resolve()
    try:
        joined = _safe_join_under(root, candidate)
    except StorageError:
        return  # rejected — the guard did its job
    assert joined.is_relative_to(root)


# ---------------------------------------------------------------------------
# One normaliser: get_object_store_prefix is normalize_key
# ---------------------------------------------------------------------------


@given(key=_clean_key, spelling=_key_spellings)
@settings(max_examples=100)
def test_get_object_store_prefix_agrees_with_normalize_key(key: str, spelling) -> None:
    """The execution-side helper and the storage normaliser never diverge."""
    from application_sdk.execution import get_object_store_prefix

    with mock.patch.object(constants, "TEMPORARY_PATH", _STAGING_ROOT):
        path = spelling(key)
        assert get_object_store_prefix(path) == normalize_key(path)


def test_get_object_store_prefix_maps_staging_root_to_store_root() -> None:
    """The staging root is the store root (``''``), not ``'.'``."""
    from application_sdk.execution import get_object_store_prefix

    with mock.patch.object(constants, "TEMPORARY_PATH", _STAGING_ROOT):
        assert get_object_store_prefix(_STAGING_ROOT) == ""
        assert get_object_store_prefix(f"{_STAGING_ROOT}/") == ""


def test_get_object_store_prefix_converts_backslashes() -> None:
    """A backslash path yields the forward-slash key on every platform."""
    from application_sdk.execution import get_object_store_prefix

    assert get_object_store_prefix("artifacts\\apps\\x\\f.json") == (
        "artifacts/apps/x/f.json"
    )


@pytest.mark.skipif(os.name != "nt", reason="drive letters exist only on Windows")
def test_normalize_key_strips_drive_letter() -> None:
    """A Windows drive is never part of a store key (runs on the Windows CI legs)."""
    assert normalize_key("C:\\Users\\runner\\raw\\records.json") == (
        "Users/runner/raw/records.json"
    )


def test_activity_utils_temporary_path_is_a_deprecated_alias() -> None:
    """The old accidental re-export still resolves, and says where it lives."""
    from application_sdk.execution._temporal import activity_utils

    with pytest.warns(DeprecationWarning, match="application_sdk.constants"):
        value = activity_utils.TEMPORARY_PATH
    assert value == constants.TEMPORARY_PATH
