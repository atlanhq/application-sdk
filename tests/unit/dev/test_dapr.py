"""Unit tests for ``application_sdk.dev._dapr``.

Covers the pure helpers (``_platform_tuple``, ``_write_components``) and
the lifecycle invariants of ``embedded_dapr`` (env-var save/restore,
components-dir cleanup) without actually downloading or spawning a
real ``daprd``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import tarfile
import tempfile
import urllib.error
import zipfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.dev import _dapr
from application_sdk.dev._dapr import (
    _COMPONENTS_YAML,
    EmbeddedDapr,
    _daprd_archive_ext,
    _daprd_binary_name,
    _download_daprd,
    _platform_tuple,
    _wait_for_dapr_ready,
    _write_components,
    embedded_dapr,
)
from application_sdk.dev._dapr_errors import (
    DaprComponentsConfigError,
    DaprComponentsDirNotFoundError,
    DaprConfigFileNotFoundError,
    DaprdDownloadIncompleteError,
    DaprdExitedError,
    UnsupportedArchitectureError,
    UnsupportedOsError,
)
from tests.unit.private_stdlib import private_stdlib


class TestPlatformTuple:
    """``_platform_tuple`` maps ``platform.system()`` + ``platform.machine()``
    to the strings Dapr uses in its release asset names."""

    @pytest.mark.parametrize(
        ("sys_name", "machine", "expected"),
        [
            ("Darwin", "arm64", ("darwin", "arm64")),
            ("Darwin", "x86_64", ("darwin", "amd64")),
            ("Linux", "x86_64", ("linux", "amd64")),
            ("Linux", "aarch64", ("linux", "arm64")),
            ("Windows", "AMD64", ("windows", "amd64")),
        ],
    )
    def test_supported_combos(
        self, sys_name: str, machine: str, expected: tuple[str, str]
    ) -> None:
        with (
            patch("platform.system", return_value=sys_name),
            patch("platform.machine", return_value=machine),
        ):
            assert _platform_tuple() == expected

    def test_unsupported_arch_raises(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="riscv64"),
            pytest.raises(UnsupportedArchitectureError),
        ):
            _platform_tuple()

    def test_unsupported_os_raises(self) -> None:
        with (
            patch("platform.system", return_value="FreeBSD"),
            patch("platform.machine", return_value="amd64"),
            pytest.raises(UnsupportedOsError),
        ):
            _platform_tuple()


class TestWriteComponents:
    """``_write_components`` materialises the YAMLs the SDK expects."""

    def test_writes_all_components(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        objectstore_root = tmp_path / "objects"
        eventstore_root = tmp_path / "events"

        _write_components(components_dir, objectstore_root, eventstore_root)

        expected = sorted(_COMPONENTS_YAML.keys())
        actual = sorted(p.name for p in components_dir.iterdir())
        assert actual == expected

    def test_pubsub_uses_in_memory(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        _write_components(components_dir, tmp_path / "objects", tmp_path / "events")
        assert "pubsub.in-memory" in (components_dir / "pubsub.yaml").read_text()

    def test_objectstore_root_substituted(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        objectstore_root = tmp_path / "objects"
        eventstore_root = tmp_path / "events"

        _write_components(components_dir, objectstore_root, eventstore_root)

        content = (components_dir / "objectstore.yaml").read_text()
        assert str(objectstore_root.resolve()) in content
        assert "bindings.localstorage" in content

    def test_eventstore_root_substituted(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        objectstore_root = tmp_path / "objects"
        eventstore_root = tmp_path / "events"

        _write_components(components_dir, objectstore_root, eventstore_root)

        content = (components_dir / "eventstore.yaml").read_text()
        assert str(eventstore_root.resolve()) in content
        assert "bindings.localstorage" in content

    def test_statestore_uses_in_memory(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        _write_components(components_dir, tmp_path / "objects", tmp_path / "events")
        assert "state.in-memory" in (components_dir / "statestore.yaml").read_text()

    def test_secretstore_uses_local_env(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        _write_components(components_dir, tmp_path / "objects", tmp_path / "events")
        assert (
            "secretstores.local.env"
            in (components_dir / "secretstore.yaml").read_text()
        )

    def test_secrets_file_emits_file_backed_secret_store(self, tmp_path: Path) -> None:
        """When ``secrets_file`` is supplied, the secret stores become file-backed."""
        components_dir = tmp_path / "components"
        secrets_file = tmp_path / "my-secrets.json"
        secrets_file.write_text('{"api-key": "abc-123"}')

        _write_components(
            components_dir,
            tmp_path / "objects",
            tmp_path / "events",
            secrets_file=str(secrets_file),
        )

        # Hyphenated keys round-trip through a file store; ``local.env`` wouldn't.
        for name in ("secretstore", "deployment-secret-store"):
            content = (components_dir / f"{name}.yaml").read_text()
            assert "secretstores.local.file" in content
            assert "secretstores.local.env" not in content
            assert str(secrets_file.resolve()) in content

    def test_secrets_file_omitted_keeps_env_store(self, tmp_path: Path) -> None:
        """Without ``secrets_file``, secret stores stay env-backed."""
        components_dir = tmp_path / "components"
        _write_components(components_dir, tmp_path / "objects", tmp_path / "events")
        assert (
            "secretstores.local.env"
            in (components_dir / "secretstore.yaml").read_text()
        )

    def test_objectstore_root_created(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        objectstore_root = tmp_path / "objects" / "nested"
        eventstore_root = tmp_path / "events" / "nested"
        assert not objectstore_root.exists()
        assert not eventstore_root.exists()

        _write_components(components_dir, objectstore_root, eventstore_root)

        assert objectstore_root.is_dir()
        assert eventstore_root.is_dir()


class TestEmbeddedDaprLifecycle:
    """``embedded_dapr`` must save/restore env vars and clean up its temp dir."""

    @pytest.fixture(autouse=True)
    def _stub_subprocess_and_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Replace binary download, subprocess spawn, readiness poll, and port
        allocation with no-ops so the context manager runs end-to-end without
        touching the network or spawning a real daprd."""
        fake_binary = tmp_path / "daprd-stub"
        fake_binary.write_text("#!/bin/sh\n")
        fake_binary.chmod(0o755)

        monkeypatch.setattr(
            "application_sdk.dev._dapr._ensure_daprd_binary",
            lambda: fake_binary,
        )

        # Stub port allocation — avoids opening a real socket in the unit suite.
        monkeypatch.setattr(
            "application_sdk.dev._dapr._pick_free_ports",
            lambda count: [13500, 15001][:count],
        )

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.wait = AsyncMock(return_value=0)
        fake_proc.terminate = MagicMock()
        fake_proc.kill = MagicMock()

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_proc

        monkeypatch.setattr(
            "application_sdk.dev._dapr._wait_for_dapr_ready",
            AsyncMock(return_value=None),
        )

        with private_stdlib("application_sdk.dev._dapr", "asyncio") as local_asyncio:
            monkeypatch.setattr(
                local_asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
            )
            yield fake_proc

    @pytest.mark.asyncio
    async def test_yields_embedded_dapr_dataclass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        async with embedded_dapr(app_id="test-app") as dapr:
            assert isinstance(dapr, EmbeddedDapr)
            assert dapr.http_port > 0
            assert dapr.grpc_port > 0
            assert Path(dapr.components_dir).is_dir()

    @pytest.mark.asyncio
    async def test_sets_env_vars_inside_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH"):
            monkeypatch.delenv(k, raising=False)

        async with embedded_dapr(app_id="test-app") as dapr:
            assert os.environ["DAPR_HTTP_PORT"] == str(dapr.http_port)
            assert os.environ["DAPR_GRPC_PORT"] == str(dapr.grpc_port)
            assert os.environ["DAPR_COMPONENTS_PATH"] == dapr.components_dir

    @pytest.mark.asyncio
    async def test_restores_prior_env_vars_on_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DAPR_HTTP_PORT", "preexisting-3500")
        monkeypatch.delenv("DAPR_GRPC_PORT", raising=False)
        monkeypatch.delenv("DAPR_COMPONENTS_PATH", raising=False)

        async with embedded_dapr(app_id="test-app"):
            pass

        # Pre-existing value comes back.
        assert os.environ["DAPR_HTTP_PORT"] == "preexisting-3500"
        # Previously-unset vars are unset again.
        assert "DAPR_GRPC_PORT" not in os.environ
        assert "DAPR_COMPONENTS_PATH" not in os.environ

    @pytest.mark.asyncio
    async def test_cleans_up_components_dir_on_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        async with embedded_dapr(app_id="test-app") as dapr:
            components_dir = Path(dapr.components_dir)
            assert components_dir.is_dir()

        assert not components_dir.exists()

    @pytest.mark.asyncio
    async def test_custom_components_dir_is_used_and_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller-supplied components_dir is used verbatim, exposed via env +
        the dataclass, left in place on exit (the caller owns it), and the env
        var is restored on exit."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DAPR_COMPONENTS_PATH", raising=False)
        custom = tmp_path / "my-components"
        custom.mkdir()
        (custom / "statestore.yaml").write_text("kind: Component\n")

        async with embedded_dapr(components_dir=str(custom)) as dapr:
            assert Path(dapr.components_dir) == custom
            assert os.environ["DAPR_COMPONENTS_PATH"] == str(custom)

        # Not deleted — and the SDK did not overwrite it with its own YAMLs.
        assert custom.is_dir()
        assert (custom / "statestore.yaml").exists()
        assert not (custom / "pubsub.yaml").exists()
        # Previously-unset env var is unset again on exit.
        assert "DAPR_COMPONENTS_PATH" not in os.environ

    @pytest.mark.asyncio
    async def test_custom_components_dir_conflicts_with_secrets_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # The guard must fire before any env mutation — snapshot and confirm.
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(DaprComponentsConfigError, match="not both"):
            async with embedded_dapr(
                components_dir=str(tmp_path), secrets_file=str(tmp_path / "s.json")
            ):
                pass
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH"):
            assert k not in os.environ

    @pytest.mark.asyncio
    async def test_missing_components_dir_fails_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-existent components_dir raises immediately, not as a slow,
        misleading readiness timeout — and before any env mutation."""
        monkeypatch.chdir(tmp_path)
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH"):
            monkeypatch.delenv(k, raising=False)
        missing = tmp_path / "does-not-exist"
        with pytest.raises(DaprComponentsDirNotFoundError):
            async with embedded_dapr(components_dir=str(missing)):
                pass
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH"):
            assert k not in os.environ

    @pytest.mark.asyncio
    async def test_config_file_is_passed_to_daprd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _stub_subprocess_and_binary,
    ) -> None:
        """A Dapr Configuration (e.g. secret scoping) reaches ``daprd --config``;
        without one, no ``--config`` flag is passed."""
        monkeypatch.chdir(tmp_path)
        config = tmp_path / "config.yaml"
        config.write_text("kind: Configuration\n")
        spawned: list[tuple[object, ...]] = []

        async def _record(*args, **_kwargs):
            spawned.append(args)
            return _stub_subprocess_and_binary

        with private_stdlib("application_sdk.dev._dapr", "asyncio") as local_asyncio:
            monkeypatch.setattr(local_asyncio, "create_subprocess_exec", _record)
            async with embedded_dapr(config_file=str(config)):
                pass
            async with embedded_dapr():
                pass

        with_config, without_config = spawned
        flag = with_config.index("--config")
        assert with_config[flag + 1] == str(config)
        assert "--config" not in without_config

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["", "   ", "missing.yaml"])
    async def test_missing_config_file_fails_fast(
        self, bad: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(DaprConfigFileNotFoundError):
            async with embedded_dapr(config_file=bad):
                pass
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH"):
            assert k not in os.environ

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_blank_components_dir_fails_fast(
        self, blank: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank/whitespace components_dir must not slip past the guard —
        Path("").is_dir() resolves to cwd and would boot daprd there. Raises
        before any env mutation."""
        monkeypatch.chdir(tmp_path)
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(DaprComponentsDirNotFoundError):
            async with embedded_dapr(components_dir=blank):
                pass
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH"):
            assert k not in os.environ

    @pytest.mark.asyncio
    async def test_terminates_subprocess_on_exit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _stub_subprocess_and_binary,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # ``returncode is None`` simulates a running process that needs SIGTERM.
        _stub_subprocess_and_binary.returncode = None

        async with embedded_dapr(app_id="test-app"):
            pass

        _stub_subprocess_and_binary.terminate.assert_called_once()
        _stub_subprocess_and_binary.wait.assert_awaited()


_BINARY_BYTES = b"\x7fELF" + b"d" * 4096


def _write_archive(dest: Path, payload: bytes = _BINARY_BYTES) -> None:
    """Write a release-shaped archive (zip on Windows, tar.gz elsewhere)."""
    name = f"daprd_dir/{_daprd_binary_name()}"
    if _daprd_archive_ext() == ".zip":
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr(name, payload)
    else:
        with tarfile.open(dest, "w:gz") as tar:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


class TestDownloadDaprd:
    """``_download_daprd`` publishes the binary atomically and retries what a
    fresh download can fix — the network fetch itself is stubbed."""

    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_dapr, "_DOWNLOAD_BACKOFF_SECONDS", 0.0)

    def test_installs_executable_binary_and_leaves_no_temp_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_dapr, "_fetch_archive", lambda _url, d: _write_archive(d))
        target = tmp_path / "cache" / _daprd_binary_name()

        _download_daprd(target)

        assert target.read_bytes() == _BINARY_BYTES
        if os.name != "nt":
            assert os.access(target, os.X_OK)
        assert sorted(p.name for p in target.parent.iterdir()) == [target.name]

    def test_retries_a_transient_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def _flaky(_url: str, dest: Path) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionResetError("peer reset")
            _write_archive(dest)

        monkeypatch.setattr(_dapr, "_fetch_archive", _flaky)
        target = tmp_path / _daprd_binary_name()

        _download_daprd(target)

        assert len(calls) == 2
        assert target.read_bytes() == _BINARY_BYTES

    def test_truncated_archive_never_lands_at_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A download cut short on every attempt raises and leaves nothing at
        *target* — the old code wrote straight to it and left a truncated
        binary that ``_ensure_daprd_binary`` accepted on every later run."""

        def _truncated(_url: str, dest: Path) -> None:
            _write_archive(dest)
            dest.write_bytes(dest.read_bytes()[:64])

        monkeypatch.setattr(_dapr, "_fetch_archive", _truncated)
        target = tmp_path / _daprd_binary_name()

        with pytest.raises(Exception) as exc_info:
            _download_daprd(target)

        assert _dapr._is_retryable_download_error(exc_info.value)
        assert list(tmp_path.iterdir()) == []

    def test_short_extraction_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_dapr, "_fetch_archive", lambda _url, d: _write_archive(d))
        real_copy = _dapr.shutil.copyfileobj

        def _short_copy(src, dst, *args) -> None:
            real_copy(io.BytesIO(src.read()[:10]), dst)

        target = tmp_path / _daprd_binary_name()

        with private_stdlib(_dapr, "shutil") as local_shutil:
            monkeypatch.setattr(local_shutil, "copyfileobj", _short_copy)
            with pytest.raises(DaprdDownloadIncompleteError):
                _download_daprd(target)
        assert not target.exists()

    def test_client_error_is_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def _not_found(url: str, _dest: Path) -> None:
            calls.append(1)
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(_dapr, "_fetch_archive", _not_found)

        with pytest.raises(urllib.error.HTTPError):
            _download_daprd(tmp_path / _daprd_binary_name())
        assert len(calls) == 1

    @pytest.mark.parametrize(
        ("code", "retryable"), [(503, True), (502, True), (429, True), (404, False)]
    )
    def test_http_status_classification(self, code: int, retryable: bool) -> None:
        err = urllib.error.HTTPError("u", code, "x", {}, None)  # type: ignore[arg-type]
        assert _dapr._is_retryable_download_error(err) is retryable


class TestWaitForDaprReady:
    @pytest.mark.asyncio
    async def test_exited_daprd_fails_fast_with_log_tail(self, tmp_path: Path) -> None:
        """A daprd that already died raises at once with its exit code and
        output, instead of being polled for the whole readiness window."""
        log = tmp_path / "daprd.log"
        log.write_text("starting\nfatal: failed to listen on port 13500\n")
        proc = MagicMock()
        proc.returncode = 1

        with pytest.raises(DaprdExitedError) as exc_info:
            await _wait_for_dapr_ready(13500, timeout_s=2.0, proc=proc, log_path=log)

        assert exc_info.value.exit_code == 1
        assert exc_info.value.log_tail is not None
        assert "failed to listen" in exc_info.value.log_tail


@contextlib.contextmanager
def _private_spawn_and_log_dir(
    monkeypatch: pytest.MonkeyPatch, spawn, log_dir: Path
) -> Iterator[None]:
    """Route ``_dapr``'s subprocess spawn to *spawn* and its temp log into
    *log_dir*, on private copies of ``asyncio`` / ``tempfile`` so no other
    caller in the worker sees either patch."""
    real_mkstemp = tempfile.mkstemp
    with (
        private_stdlib(_dapr, "asyncio") as local_asyncio,
        private_stdlib(_dapr, "tempfile") as local_tempfile,
    ):
        monkeypatch.setattr(local_asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            local_tempfile,
            "mkstemp",
            lambda **kw: real_mkstemp(**{**kw, "dir": str(log_dir)}),
        )
        yield


class TestEmbeddedDaprLog:
    """daprd output goes to a temp log that is kept only when daprd died."""

    @pytest.fixture(autouse=True)
    def _stub(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "application_sdk.dev._dapr._ensure_daprd_binary", lambda: tmp_path / "d"
        )
        monkeypatch.setattr(
            "application_sdk.dev._dapr._pick_free_ports",
            lambda count: [13500, 15001][:count],
        )
        proc = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        self.spawn_kwargs: dict = {}

        async def _spawn(*_args, **kwargs):
            self.spawn_kwargs = kwargs
            return proc

        self.log_dir = tmp_path / "logs"
        self.log_dir.mkdir()
        with _private_spawn_and_log_dir(monkeypatch, _spawn, self.log_dir):
            yield

    @pytest.mark.asyncio
    async def test_log_removed_after_clean_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.dev._dapr._wait_for_dapr_ready", AsyncMock()
        )
        async with embedded_dapr():
            pass
        assert self.spawn_kwargs["stdout"] is not asyncio.subprocess.DEVNULL
        assert not [p for p in self.log_dir.iterdir() if p.suffix == ".log"]

    @pytest.mark.asyncio
    async def test_log_kept_when_daprd_died(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.dev._dapr._wait_for_dapr_ready",
            AsyncMock(side_effect=DaprdExitedError(exit_code=1)),
        )
        with pytest.raises(DaprdExitedError):
            async with embedded_dapr():
                pass
        assert [p for p in self.log_dir.iterdir() if p.suffix == ".log"]


class TestEnsureDaprdBinary:
    """An existing cached binary is trusted only if it runs."""

    @pytest.fixture
    def cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(_dapr, "_CACHE_DIR", tmp_path)
        self.downloads: list[Path] = []

        def _fake_download(target: Path) -> None:
            self.downloads.append(target)
            target.write_bytes(_BINARY_BYTES)

        monkeypatch.setattr(_dapr, "_download_daprd", _fake_download)
        return tmp_path / _daprd_binary_name()

    def test_working_cached_binary_is_reused(
        self, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache.write_bytes(b"ok")
        monkeypatch.setattr(_dapr, "_daprd_runs", lambda _b: True)
        assert _dapr._ensure_daprd_binary() == cache
        assert self.downloads == []

    def test_broken_cached_binary_is_replaced(
        self, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated binary left by the pre-atomic download path is not
        accepted just because it exists."""
        cache.write_bytes(b"trunc")
        monkeypatch.setattr(_dapr, "_daprd_runs", lambda _b: False)
        assert _dapr._ensure_daprd_binary() == cache
        assert self.downloads == [cache]
        assert cache.read_bytes() == _BINARY_BYTES

    def test_missing_binary_is_downloaded(self, cache: Path) -> None:
        assert _dapr._ensure_daprd_binary() == cache
        assert self.downloads == [cache]

    @pytest.mark.parametrize(
        ("returncode", "stdout", "expected"),
        [(0, f"{_dapr._DAPRD_VERSION}\n", True), (0, "0.0.1\n", False), (1, "", False)],
    )
    def test_daprd_runs_checks_exit_and_version(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        returncode: int,
        stdout: str,
        expected: bool,
    ) -> None:
        with private_stdlib(_dapr, "subprocess") as local_subprocess:
            monkeypatch.setattr(
                local_subprocess,
                "run",
                lambda *_a, **_k: MagicMock(returncode=returncode, stdout=stdout),
            )
            assert _dapr._daprd_runs(tmp_path / "daprd") is expected

    def test_daprd_runs_false_when_not_executable(self, tmp_path: Path) -> None:
        garbage = tmp_path / "daprd"
        garbage.write_bytes(b"not a binary")
        assert _dapr._daprd_runs(garbage) is False


class TestSpawnFailureCleanup:
    @pytest.mark.asyncio
    async def test_log_removed_when_spawn_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # embedded_dapr sets these before spawning; register them so monkeypatch
        # restores them after this test.
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(
            "application_sdk.dev._dapr._ensure_daprd_binary", lambda: tmp_path / "d"
        )
        monkeypatch.setattr(
            "application_sdk.dev._dapr._pick_free_ports",
            lambda count: [13500, 15001][:count],
        )

        # Not an OSError: a failed unlink (PermissionError on Windows when the
        # log is still open) must not be able to stand in for this error.
        async def _spawn_fails(*_a, **_k):
            raise RuntimeError("cannot execute")

        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        with (
            _private_spawn_and_log_dir(monkeypatch, _spawn_fails, log_dir),
            pytest.raises(RuntimeError, match="cannot execute"),
        ):
            async with embedded_dapr():
                pass
        assert not [p for p in log_dir.iterdir() if p.suffix == ".log"]
