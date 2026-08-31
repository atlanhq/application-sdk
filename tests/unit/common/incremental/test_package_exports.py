"""The published surface of ``application_sdk.common.incremental``.

The package re-exports its seam lazily through :pep:`562` ``__getattr__``, and
the conformance suite (``P048``/``P049``) tells apps to import
``get_persistent_s3_prefix`` and friends *from the package* rather than from
``.helpers`` / ``.marker``.  Every other test in this directory imports the
submodules directly, so nothing exercised the package-level path: a typo in
``_EXPORTS``, a name dropped from ``__all__``, or a ``__getattr__`` that stopped
resolving would leave the whole suite green while every app that followed the
documented import broke at startup.

These tests pin that path — the names, their identity with the submodule
originals, the ``AttributeError`` contract, ``__dir__``, the resolution cache,
and the laziness itself (asserted in a subprocess, since by the time any other
test has run the storage stack is already imported).
"""

from __future__ import annotations

import subprocess
import sys
from importlib import import_module

import pytest

from application_sdk.common import incremental

# Public name -> the submodule that defines it. Written out rather than read
# from ``incremental._EXPORTS`` so the test fails when the mapping drifts,
# instead of agreeing with whatever the mapping happens to say.
_EXPECTED_ORIGINS = {
    "create_next_marker": "marker",
    "extract_epoch_id_from_qualified_name": "helpers",
    "fetch_marker_from_storage": "marker",
    "get_persistent_artifacts_path": "helpers",
    "get_persistent_s3_prefix": "helpers",
    "persist_marker_to_storage": "marker",
    "process_marker_timestamp": "marker",
}


def test_all_matches_the_expected_surface() -> None:
    assert sorted(incremental.__all__) == sorted(_EXPECTED_ORIGINS)


def test_all_is_sorted_and_free_of_duplicates() -> None:
    # ``__all__`` is read statically by the capability-manifest extractor, so
    # its literal form is part of the contract, not just its contents.
    assert incremental.__all__ == sorted(set(incremental.__all__))


def test_exports_mapping_covers_exactly_all() -> None:
    assert incremental._EXPORTS == _EXPECTED_ORIGINS


@pytest.mark.parametrize(("name", "module_name"), sorted(_EXPECTED_ORIGINS.items()))
def test_package_export_is_the_submodule_object(name: str, module_name: str) -> None:
    """``from application_sdk.common.incremental import <name>`` resolves.

    Identity, not merely existence: a re-export that resolved to a *different*
    object than ``helpers``/``marker`` defines would let the seam fork inside
    the SDK itself.
    """
    submodule = import_module(f"application_sdk.common.incremental.{module_name}")
    assert getattr(incremental, name) is getattr(submodule, name)


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="has no attribute 'not_a_seam_symbol'"):
        incremental.not_a_seam_symbol  # noqa: B018


def test_dir_includes_the_lazy_exports() -> None:
    listed = dir(incremental)
    assert set(incremental.__all__) <= set(listed)
    assert listed == sorted(set(listed))


def test_resolution_is_cached_in_module_globals() -> None:
    """First access caches, so later lookups skip ``__getattr__`` entirely."""
    name = "get_persistent_s3_prefix"
    incremental.__dict__.pop(name, None)
    resolved = getattr(incremental, name)
    assert incremental.__dict__[name] is resolved


def test_importing_the_package_does_not_pull_in_storage() -> None:
    """The laziness the module docstring promises.

    ``helpers`` and ``marker`` both import ``application_sdk.storage``. Eager
    re-exports would put the whole storage stack behind
    ``from application_sdk.common.incremental.incremental_errors import ...`` —
    a module that deliberately depends on nothing but the error leaves. Run in
    a subprocess because this process has long since imported storage.
    """
    code = (
        "import sys\n"
        "import application_sdk.common.incremental as m\n"
        "assert 'application_sdk.common.incremental.helpers' not in sys.modules\n"
        "assert 'application_sdk.common.incremental.marker' not in sys.modules\n"
        "m.get_persistent_s3_prefix\n"
        "assert 'application_sdk.common.incremental.helpers' in sys.modules\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
