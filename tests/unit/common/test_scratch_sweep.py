"""The startup sweep of stale scratch under ``TEMPORARY_PATH``."""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path

import pytest

from application_sdk import constants
from application_sdk.common import scratch_sweep
from application_sdk.common.scratch_sweep import SweepStatus, sweep_temporary_path


class _RecordingLogger:
    """Captures what the module logged, by level, with arguments applied."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def __getattr__(self, level: str):
        def log(msg: str, *args: object, **_kwargs: object) -> None:
            self.rows.append((level, msg % args if args else msg))

        return log

    def at(self, level: str) -> list[str]:
        return [text for lvl, text in self.rows if lvl == level]


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    recorder = _RecordingLogger()
    monkeypatch.setattr(scratch_sweep, "logger", recorder)
    return recorder


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A ``TEMPORARY_PATH`` stand-in, deep enough to pass the depth guard."""
    path = tmp_path / "app" / "local" / "tmp"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def symlinks(tmp_path: Path) -> None:
    """Skip where this process cannot create symbolic links (unprivileged Windows)."""
    probe = tmp_path / "symlink-probe"
    try:
        os.symlink(tmp_path, probe)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    probe.unlink()


def _write(path: Path, data: bytes = b"scratch") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sweep(root: Path, *keep: str):
    return sweep_temporary_path(root, enabled=True, keep=keep)


def test_removes_files_and_nested_directories(root: Path) -> None:
    """A dead process's files and directory trees, however deep, all go."""
    _write(root / "loose.parquet")
    _write(root / "tmpa1b2c3" / "nested" / "deeper" / "chunk-0.json")
    _write(root / "duckdb-spill" / "duckdb_temp_block-1.block")
    (root / "empty-dir").mkdir()

    result = _sweep(root)

    assert result.status is SweepStatus.SWEPT
    assert list(root.iterdir()) == []
    # 3 files + 5 directories: tmpa1b2c3, nested, deeper, duckdb-spill, empty-dir.
    assert result.removed_entries == 8
    assert result.failed_entries == 0


def test_keeps_the_root_itself(root: Path) -> None:
    """``TEMPORARY_PATH`` survives, empty, whether or not it held anything."""
    _write(root / "a" / "b.txt")

    _sweep(root)
    assert root.is_dir()
    assert list(root.iterdir()) == []

    result = _sweep(root)  # and an empty root is a no-op, not an error
    assert root.is_dir()
    assert result.status is SweepStatus.SWEPT
    assert result.removed_entries == 0


def test_counts_the_disk_space_it_freed(root: Path) -> None:
    """Random bytes, so no filesystem compression can shrink the allocation."""
    _write(root / "spill.tmp", os.urandom(64 * 1024))

    result = _sweep(root)

    assert result.removed_bytes >= 64 * 1024


def test_logs_one_info_line_with_the_counts(root: Path, logs: _RecordingLogger) -> None:
    """One INFO line, however many entries, and no warning when all went well."""
    for index in range(20):
        _write(root / f"dir-{index}" / "file.bin")

    _sweep(root)

    info = logs.at("info")
    assert len(info) == 1
    assert "removed_entries=40" in info[0]
    assert "removed_bytes=" in info[0]
    assert "failed=0" in info[0]
    assert logs.at("warning") == []


def test_keep_patterns_protect_matching_entries_at_any_depth(root: Path) -> None:
    """A kept entry is kept whole, its parents survive, and its siblings go."""
    _write(root / "connection-cache" / "conn.sqlite")
    _write(root / "models" / "a.onnx")
    _write(root / "models" / "notes.txt")
    _write(root / "runs" / "r1" / "keep-me" / "state.json")
    _write(root / "runs" / "r1" / "spill.bin")
    _write(root / "runs" / "r2" / "spill.bin")

    result = _sweep(root, "connection-cache", "models/*.onnx", "runs/*/keep-me")

    assert (root / "connection-cache" / "conn.sqlite").exists()
    assert (root / "models" / "a.onnx").exists()
    assert not (root / "models" / "notes.txt").exists()
    assert (root / "runs" / "r1" / "keep-me" / "state.json").exists()
    assert not (root / "runs" / "r1" / "spill.bin").exists()
    assert not (root / "runs" / "r2").exists()
    assert result.kept_entries == 3


def test_a_keep_glob_never_crosses_a_slash(root: Path) -> None:
    """``*/keep.txt`` names depth two only; it keeps nothing deeper."""
    _write(root / "a" / "keep.txt")
    _write(root / "a" / "b" / "keep.txt")

    _sweep(root, "*/keep.txt")

    assert (root / "a" / "keep.txt").exists()
    assert not (root / "a" / "b").exists()


def test_a_leading_dot_slash_and_a_trailing_slash_do_not_matter(root: Path) -> None:
    """``./cache/`` and ``cache`` name the same entry."""
    _write(root / "cache" / "entry.bin")

    _sweep(root, " ./cache/ ")

    assert (root / "cache" / "entry.bin").exists()


def test_blank_keep_patterns_are_ignored(root: Path) -> None:
    """A trailing comma in the variable is not a pattern, and not a refusal."""
    _write(root / "stale.bin")

    result = _sweep(root, "", "   ")

    assert result.status is SweepStatus.SWEPT
    assert not (root / "stale.bin").exists()


@pytest.mark.parametrize("pattern", ["/abs/cache", "../outside", "cache/../..", "./"])
def test_an_unusable_keep_pattern_refuses_the_whole_sweep(
    root: Path, logs: _RecordingLogger, pattern: str
) -> None:
    """A keep-list it cannot honour could delete what it was meant to protect."""
    precious = _write(root / "cache" / "entry.bin")

    result = _sweep(root, "fine", pattern)

    assert result.status is SweepStatus.REFUSED
    assert precious.exists()
    assert len(logs.at("warning")) == 1
    assert pattern.strip() in logs.at("warning")[0]


def test_builtin_keep_list_protects_telemetry_staging(root: Path) -> None:
    """Live sinks write there while the sweep runs; the run outputs beside it go."""
    staged = _write(
        root
        / "artifacts/apps/my-app/production/observability/non-sdr/logs"
        / "year=2026/month=10/day=16/hour=09/batch.json.gz"
    )
    _write(root / "artifacts/apps/my-app/workflows/wf-1/run-1/raw/table/chunk-0.json")

    result = _sweep(root)

    assert staged.exists()
    assert not (root / "artifacts/apps/my-app/workflows").exists()
    assert result.kept_entries == 1


def test_builtin_keep_list_protects_lost_and_found(root: Path) -> None:
    """The filesystem's own directory when a volume is mounted at the root."""
    _write(root / "lost+found" / "#12345")
    _write(root / "stale.bin")

    _sweep(root)

    assert (root / "lost+found" / "#12345").exists()
    assert not (root / "stale.bin").exists()


def test_the_temporary_file_directory_is_kept_when_it_lies_inside(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python never recreates its temp dir, so removing it breaks every later one."""
    systmp = root / "system-tmp"
    systmp.mkdir()
    _write(systmp / "tmpxyz")
    _write(root / "stale.bin")
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))

    _sweep(root)

    assert (systmp / "tmpxyz").exists()
    assert not (root / "stale.bin").exists()


def test_off_touches_nothing_and_says_so(root: Path, logs: _RecordingLogger) -> None:
    """Disabled, it looks at nothing, and one INFO line says it was off."""
    precious = _write(root / "file.bin")

    result = sweep_temporary_path(root, enabled=False)

    assert result.status is SweepStatus.OFF
    assert precious.exists()
    assert len(logs.at("info")) == 1
    assert "off" in logs.at("info")[0]


def test_unset_arguments_follow_the_constants(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root, the switch and the keep-list all default to ``constants``."""
    _write(root / "cache" / "entry.bin")
    _write(root / "stale.bin")
    monkeypatch.setattr(constants, "TEMPORARY_PATH", str(root))
    monkeypatch.setattr(constants, "TEMPORARY_PATH_SWEEP_KEEP", ("cache",))

    monkeypatch.setattr(constants, "TEMPORARY_PATH_SWEEP_ON_START", False)
    assert sweep_temporary_path().status is SweepStatus.OFF
    assert (root / "stale.bin").exists()

    monkeypatch.setattr(constants, "TEMPORARY_PATH_SWEEP_ON_START", True)
    assert sweep_temporary_path().status is SweepStatus.SWEPT
    assert not (root / "stale.bin").exists()
    assert (root / "cache" / "entry.bin").exists()


def test_a_missing_root_is_nothing_to_sweep(tmp_path: Path) -> None:
    """A fresh volume has nothing to clear, and the sweep does not create it."""
    missing = tmp_path / "app" / "local" / "tmp"

    result = _sweep(missing)

    assert result.status is SweepStatus.NOTHING_TO_SWEEP
    assert not missing.exists()


def test_a_root_that_is_a_file_is_nothing_to_sweep(tmp_path: Path) -> None:
    """A misconfigured root that is a file is left alone, not deleted."""
    not_a_dir = _write(tmp_path / "app" / "local" / "tmp")

    result = _sweep(not_a_dir)

    assert result.status is SweepStatus.NOTHING_TO_SWEEP
    assert not_a_dir.exists()


class TestDangerousRoots:
    """Each guard refuses, logs one warning, and deletes nothing."""

    def _assert_refused(self, target: Path, logs: _RecordingLogger) -> None:
        result = _sweep(target)
        assert result.status is SweepStatus.REFUSED
        assert len(logs.at("warning")) == 1

    def test_the_filesystem_root(self, tmp_path: Path, logs: _RecordingLogger) -> None:
        """``/`` (or a drive root) is never swept."""
        self._assert_refused(Path(tmp_path.anchor), logs)

    def test_a_path_with_one_component(
        self, tmp_path: Path, logs: _RecordingLogger
    ) -> None:
        """``/tmp``-shaped roots are refused before they are ever looked at."""
        self._assert_refused(Path(tmp_path.anchor) / "scratch-sweep-test", logs)

    @pytest.mark.parametrize("target", ["home", "parent-of-home"])
    def test_the_home_directory_or_a_parent_of_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: _RecordingLogger,
        target: str,
    ) -> None:
        """A root that is, or contains, the home directory is refused."""
        home = tmp_path / "homes" / "worker"
        precious = _write(home / ".profile")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))  # what Windows reads

        self._assert_refused(home if target == "home" else home.parent, logs)
        assert precious.exists()

    @pytest.mark.parametrize("target", ["cwd", "parent-of-cwd"])
    def test_the_working_directory_or_a_parent_of_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: _RecordingLogger,
        target: str,
    ) -> None:
        """A root that is, or contains, the app's working directory is refused."""
        app_root = tmp_path / "srv" / "app"
        precious = _write(app_root / "main.py")
        monkeypatch.chdir(app_root)

        self._assert_refused(app_root if target == "cwd" else app_root.parent, logs)
        assert precious.exists()

    def test_a_symlink_that_resolves_to_the_home_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        logs: _RecordingLogger,
        symlinks: None,
    ) -> None:
        """The guards judge where the path leads, not what it is called."""
        home = tmp_path / "homes" / "worker"
        precious = _write(home / ".profile")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        disguised = tmp_path / "app" / "local" / "tmp"
        disguised.parent.mkdir(parents=True)
        os.symlink(home, disguised)

        self._assert_refused(disguised, logs)
        assert precious.exists()

    def test_no_home_directory_is_not_a_reason_to_refuse(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An arbitrary container UID often has no home to compare against."""
        _write(root / "stale.bin")
        monkeypatch.setattr(os.path, "expanduser", lambda path: path)

        assert _sweep(root).status is SweepStatus.SWEPT
        assert not (root / "stale.bin").exists()


class TestLinks:
    """A link is unlinked; whatever it points at is never reached."""

    def test_links_are_removed_and_their_targets_survive(
        self, tmp_path: Path, root: Path, symlinks: None
    ) -> None:
        """Links to a directory, to a file and to nothing all go; targets stay."""
        outside = tmp_path / "outside"
        precious = _write(outside / "precious.txt")
        os.symlink(outside, root / "link-to-dir")
        os.symlink(precious, root / "link-to-file")
        os.symlink(tmp_path / "nowhere", root / "dangling")

        result = _sweep(root)

        assert list(root.iterdir()) == []
        assert precious.read_bytes() == b"scratch"
        assert result.removed_entries == 3
        assert result.failed_entries == 0

    def test_a_link_deep_in_a_tree_is_not_followed_either(
        self, tmp_path: Path, root: Path, symlinks: None
    ) -> None:
        """Depth changes nothing: a nested link is unlinked, not descended."""
        outside = tmp_path / "outside"
        precious = _write(outside / "precious.txt")
        (root / "a" / "b").mkdir(parents=True)
        os.symlink(outside, root / "a" / "b" / "escape")

        _sweep(root)

        assert not (root / "a").exists()
        assert precious.exists()

    def test_a_symlinked_root_is_swept_where_it_leads(
        self, tmp_path: Path, symlinks: None
    ) -> None:
        """A root that is itself a link is swept at its target; the link stays."""
        real = tmp_path / "volume" / "scratch"
        _write(real / "stale.bin")
        link = tmp_path / "app" / "local" / "tmp"
        link.parent.mkdir(parents=True)
        os.symlink(real, link)

        _sweep(link)

        assert real.is_dir()
        assert list(real.iterdir()) == []
        assert link.is_symlink()


class TestMountPoints:
    """Another filesystem mounted inside the root is not this pod's scratch."""

    def test_a_directory_on_another_device_is_left_whole(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mount point is kept with its contents; its siblings still go."""
        mounted = root / "mnt" / "cache"
        precious = _write(mounted / "data.bin")
        _write(root / "mnt" / "stale.bin")
        real_device_of = scratch_sweep._device_of

        def device_of(path: str) -> int:
            device = real_device_of(path)
            return device + 1 if Path(path) == mounted.resolve() else device

        monkeypatch.setattr(scratch_sweep, "_device_of", device_of)

        result = _sweep(root)

        assert precious.exists()
        assert not (root / "mnt" / "stale.bin").exists()
        assert result.kept_entries == 1
        assert result.failed_entries == 0

    def test_a_directory_that_cannot_be_inspected_is_left_alone(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What cannot be identified is kept rather than deleted."""
        opaque = root / "opaque"
        precious = _write(opaque / "data.bin")
        real_device_of = scratch_sweep._device_of

        def device_of(path: str) -> int:
            if Path(path) == opaque.resolve():
                raise FileNotFoundError(errno.ENOENT, "gone", path)
            return real_device_of(path)

        monkeypatch.setattr(scratch_sweep, "_device_of", device_of)

        result = _sweep(root)

        assert precious.exists()
        assert result.kept_entries == 1


class TestFailures:
    """Nothing the sweep runs into may stop the worker from starting."""

    def test_an_entry_that_cannot_be_removed_is_counted_once_and_reported_once(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, logs: _RecordingLogger
    ) -> None:
        """The rest is still swept, and one WARNING names the error type."""
        stuck = _write(root / "dir" / "stuck.bin")
        _write(root / "dir" / "free.bin")
        _write(root / "other.bin")
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if str(path).endswith("stuck.bin"):
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", unlink)

        result = _sweep(root)

        assert result.status is SweepStatus.SWEPT
        assert stuck.exists()
        assert not (root / "dir" / "free.bin").exists()
        assert not (root / "other.bin").exists()
        # Its parent is not retried just to fail again as "directory not empty".
        assert result.failed_entries == 1
        warnings = logs.at("warning")
        assert len(warnings) == 1
        assert "PermissionError" in warnings[0]

    def test_a_directory_that_cannot_be_listed_is_counted_once(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, logs: _RecordingLogger
    ) -> None:
        """It is not then retried as a directory to remove."""
        locked = root / "locked"
        inside = _write(locked / "inside.bin")
        _write(root / "free.bin")
        real_scandir = os.scandir

        def scandir(path="."):
            if str(path).endswith("locked"):
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", scandir)

        result = _sweep(root)

        assert inside.exists()
        assert not (root / "free.bin").exists()
        assert result.failed_entries == 1
        assert len(logs.at("warning")) == 1

    def test_a_directory_that_cannot_be_removed_is_counted_once(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A busy directory is emptied, kept, and counted once."""
        stubborn = root / "outer" / "stubborn"
        _write(stubborn / "file.bin")
        real_rmdir = os.rmdir

        def rmdir(path, *args, **kwargs):
            if str(path).endswith("stubborn"):
                raise OSError(errno.EBUSY, "Device or resource busy", str(path))
            return real_rmdir(path, *args, **kwargs)

        monkeypatch.setattr(os, "rmdir", rmdir)

        result = _sweep(root)

        assert stubborn.is_dir()
        assert not (stubborn / "file.bin").exists()
        # The parent it pins is not retried, so one failure is one count.
        assert result.failed_entries == 1

    def test_an_unexpected_error_is_logged_once_and_never_raised(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, logs: _RecordingLogger
    ) -> None:
        """A bug in the walk ends the sweep, not the worker."""

        def broken(*_args: object) -> None:
            raise RuntimeError("a bug in the walk")

        monkeypatch.setattr(scratch_sweep, "_delete_children", broken)

        result = _sweep(root)

        assert result.status is SweepStatus.FAILED
        warnings = logs.at("warning")
        assert len(warnings) == 1
        assert "RuntimeError" in warnings[0]
