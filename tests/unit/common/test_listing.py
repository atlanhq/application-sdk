"""Direct tests for ``application_sdk.common._listing.safe_list_directory``.

Pin the primitive's contract directly so a future regression cannot
slip through unless one of the three caller test files happens to
catch it. Covers happy paths, symlink exclusion, OSError surfacing,
the platform-specific fsync barrier, and iterative-descent invariants.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk.common._listing import (
    _DARWIN_F_FULLFSYNC,
    _flush_directory_metadata,
    safe_list_directory,
)

# =============================================================================
# safe_list_directory — happy paths
# =============================================================================


class TestSafeListDirectoryHappyPath:
    def test_flat_directory_returns_all_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        (tmp_path / "c.txt").write_bytes(b"c")

        result = safe_list_directory(tmp_path)

        assert {p.name for p in result} == {"a.txt", "b.txt", "c.txt"}
        assert all(p.is_file() for p in result)

    def test_nested_directories_recursed(self, tmp_path: Path) -> None:
        """A 3-deep tree: files at every level should appear."""
        (tmp_path / "root.txt").write_bytes(b"root")
        level1 = tmp_path / "L1"
        level1.mkdir()
        (level1 / "level1.txt").write_bytes(b"L1")
        level2 = level1 / "L2"
        level2.mkdir()
        (level2 / "level2.txt").write_bytes(b"L2")
        level3 = level2 / "L3"
        level3.mkdir()
        (level3 / "level3.txt").write_bytes(b"L3")

        result = safe_list_directory(tmp_path)

        assert {p.name for p in result} == {
            "root.txt",
            "level1.txt",
            "level2.txt",
            "level3.txt",
        }

    def test_empty_directory_returns_empty_list(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert safe_list_directory(empty) == []

    def test_skips_subdirectories_with_no_files(self, tmp_path: Path) -> None:
        """Subdirectories themselves are not items — only files are."""
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "empty_sub").mkdir()
        (tmp_path / "nonempty_sub").mkdir()
        (tmp_path / "nonempty_sub" / "b.txt").write_bytes(b"b")

        result = safe_list_directory(tmp_path)

        assert {p.name for p in result} == {"a.txt", "b.txt"}


# =============================================================================
# safe_list_directory — symlink behavior
# =============================================================================


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
class TestSafeListDirectorySymlinks:
    def test_symlink_to_file_is_not_followed(self, tmp_path: Path) -> None:
        """Behavior change vs Path.rglob: symlink-to-file is excluded.

        ``Path.rglob + Path.is_file()`` follows symlinks by default
        and would include the link. ``safe_list_directory`` uses
        ``follow_symlinks=False`` and excludes it.
        """
        # Real file lives in an outside dir; symlink points to it
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        target_file = target_dir / "real.txt"
        target_file.write_bytes(b"x")

        scan_dir = tmp_path / "scan"
        scan_dir.mkdir()
        (scan_dir / "regular.txt").write_bytes(b"y")
        (scan_dir / "link_to_file").symlink_to(target_file)

        result = safe_list_directory(scan_dir)

        # Only the regular file — the symlink is excluded
        assert {p.name for p in result} == {"regular.txt"}

    def test_symlink_to_directory_is_not_followed(self, tmp_path: Path) -> None:
        """A symlink-to-directory is not recursed into — its contents
        do not appear in the listing."""
        external = tmp_path / "external"
        external.mkdir()
        (external / "hidden.txt").write_bytes(b"hidden")

        scan_dir = tmp_path / "scan"
        scan_dir.mkdir()
        (scan_dir / "regular.txt").write_bytes(b"y")
        (scan_dir / "link_to_dir").symlink_to(external, target_is_directory=True)

        result = safe_list_directory(scan_dir)

        assert {p.name for p in result} == {"regular.txt"}

    def test_cyclic_symlink_does_not_infinite_loop(self, tmp_path: Path) -> None:
        """A directory containing a symlink to itself terminates the
        walk; symlinks-to-dirs are not followed."""
        scan_dir = tmp_path / "scan"
        scan_dir.mkdir()
        (scan_dir / "real.txt").write_bytes(b"r")
        (scan_dir / "cycle").symlink_to(scan_dir, target_is_directory=True)

        # If this hangs, the symlink-not-followed defense is broken.
        result = safe_list_directory(scan_dir)

        assert {p.name for p in result} == {"real.txt"}


# =============================================================================
# safe_list_directory — error surfacing
# =============================================================================


class TestSafeListDirectoryErrorSurfacing:
    def test_missing_path_raises_oserror(self, tmp_path: Path) -> None:
        """Unlike Path.rglob (which silently returns an empty iterator
        on some failures), safe_list_directory surfaces OSError.
        """
        missing = tmp_path / "does_not_exist"
        with pytest.raises(OSError):
            safe_list_directory(missing)

    def test_file_path_raises_oserror(self, tmp_path: Path) -> None:
        """Passing a regular file (not a directory) raises OSError."""
        f = tmp_path / "is_a_file.txt"
        f.write_bytes(b"x")
        with pytest.raises(OSError):
            safe_list_directory(f)


# =============================================================================
# _flush_directory_metadata — platform-specific barrier
# =============================================================================


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-specific")
class TestFlushDirectoryMetadataDarwin:
    def test_calls_f_fullfsync_on_darwin(self, tmp_path: Path) -> None:
        """Darwin path uses ``fcntl(F_FULLFSYNC=51)``, not plain ``fsync``."""
        import fcntl

        with patch.object(fcntl, "fcntl", return_value=0) as mock_fcntl:
            _flush_directory_metadata(tmp_path)

        assert mock_fcntl.called
        # First positional arg is the FD; second is the op.
        op_arg = mock_fcntl.call_args[0][1]
        assert op_arg == _DARWIN_F_FULLFSYNC == 51

    def test_falls_back_to_fsync_when_f_fullfsync_fails(self, tmp_path: Path) -> None:
        """If F_FULLFSYNC raises OSError (some devices don't honor it),
        the primitive falls through to portable ``os.fsync``."""
        import fcntl

        with (
            patch.object(fcntl, "fcntl", side_effect=OSError("unsupported")),
            patch.object(os, "fsync") as mock_fsync,
        ):
            _flush_directory_metadata(tmp_path)

        assert mock_fsync.called


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
class TestFlushDirectoryMetadataSwallow:
    def test_swallows_oserror_from_fsync(self, tmp_path: Path) -> None:
        """Best-effort barrier: an OSError from os.fsync must not
        propagate — the os.scandir recursion is the actual correctness
        layer below."""
        with patch.object(os, "fsync", side_effect=OSError("ro fs")):
            # Must not raise
            _flush_directory_metadata(tmp_path)

    def test_swallows_oserror_from_open(self, tmp_path: Path) -> None:
        """If we cannot even open the directory FD, the function
        returns silently — the subsequent scandir call will produce
        a more informative error in the caller."""
        with patch.object(os, "open", side_effect=OSError("eacces")):
            # Must not raise
            _flush_directory_metadata(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="windows-specific")
class TestFlushDirectoryMetadataWindows:
    def test_is_noop_on_windows(self, tmp_path: Path) -> None:
        """Windows path is a no-op (no POSIX dir FD, NTFS not
        affected). The primitive returns without opening anything."""
        with patch.object(os, "open") as mock_open:
            _flush_directory_metadata(tmp_path)
        assert not mock_open.called


# =============================================================================
# Iterative-descent invariants
# =============================================================================
# The walk uses an explicit stack. These pin invariants that a future refactor
# to a recursive form would silently regress (one-FD-at-a-time, no recursion
# limit, order-agnostic).


class TestSafeListDirectoryIterativeInvariants:
    def test_walks_substantial_depth(self, tmp_path: Path) -> None:
        """Depth far beyond any real SDK output tree must still
        complete — iterative descent has no recursion-limit ceiling.
        Single-char dir names keep the cumulative path under PATH_MAX.
        """
        # 100 levels of /d/d/d/... — far beyond any SDK tree, and
        # past the point where small bugs in the iterative form
        # (e.g. wrong stack push/pop, off-by-one in depth tracking)
        # would surface.
        DEPTH = 100
        leaf = tmp_path
        for _ in range(DEPTH):
            leaf = leaf / "d"
            leaf.mkdir()
        (leaf / "deep.txt").write_bytes(b"found me")

        result = safe_list_directory(tmp_path)

        assert len(result) == 1
        assert result[0].name == "deep.txt"

    def test_holds_at_most_one_scandir_context_at_a_time(self, tmp_path: Path) -> None:
        """Peak concurrent ``os.scandir`` contexts must be 1. A
        regression to recursive ``yield from`` would push peak to the
        tree depth (each enclosing context stays open while inner
        generators run).
        """
        # Tree with a few levels of nesting and several siblings per
        # level, so we have multiple opportunities for FDs to pile up
        # under a recursive walk.
        for top in ("a", "b", "c"):
            d1 = tmp_path / top
            d1.mkdir()
            (d1 / "f.txt").write_bytes(b"x")
            for mid in ("1", "2"):
                d2 = d1 / mid
                d2.mkdir()
                (d2 / "g.txt").write_bytes(b"y")
                d3 = d2 / "deep"
                d3.mkdir()
                (d3 / "h.txt").write_bytes(b"z")

        active = 0
        peak = 0
        real_scandir = os.scandir

        class _Tracking:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                self._inner.__enter__()
                return self._inner

            def __exit__(self, *exc):
                nonlocal active
                active -= 1
                return self._inner.__exit__(*exc)

            def __iter__(self):
                return iter(self._inner)

        def _wrapped_scandir(p):
            return _Tracking(real_scandir(p))

        with patch.object(os, "scandir", side_effect=_wrapped_scandir):
            result = safe_list_directory(tmp_path)

        # Sanity: the walk actually found everything.
        assert len(result) == 3 + 3 * 2 + 3 * 2  # top + mid + deep files
        # The key invariant: at most one scandir context open at once.
        assert peak == 1, (
            f"Iterative descent should hold at most 1 scandir context, "
            f"peak observed = {peak}. A regression to recursive yield-from "
            f"would push this to the tree depth."
        )

    def test_listing_does_not_depend_on_sibling_order(self, tmp_path: Path) -> None:
        """Callers must not depend on the order siblings are visited —
        this pins that contract via set equality.
        """
        # Names chosen so alphabetical order would clash with LIFO
        # order if the filesystem iteration ordering happens to be
        # alphabetical.
        for name in ("zzz_dir", "mmm_dir", "aaa_dir"):
            d = tmp_path / name
            d.mkdir()
            (d / "file.txt").write_bytes(name.encode())

        result = safe_list_directory(tmp_path)
        names_only = {p.parent.name for p in result}

        assert names_only == {"zzz_dir", "mmm_dir", "aaa_dir"}
