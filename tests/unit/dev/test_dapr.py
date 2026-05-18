"""Unit tests for ``application_sdk.dev._dapr``.

Covers the pure helpers (``_platform_tuple``, ``_write_components``) and
the lifecycle invariants of ``embedded_dapr`` (env-var save/restore,
components-dir cleanup) without actually downloading or spawning a
real ``daprd``.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.dev._dapr import (
    _COMPONENTS_YAML,
    EmbeddedDapr,
    _pick_free_port,
    _platform_tuple,
    _write_components,
    embedded_dapr,
)
from application_sdk.dev._dapr_errors import (
    UnsupportedArchitectureError,
    UnsupportedOsError,
)


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
    """``_write_components`` materialises the five YAMLs the SDK expects."""

    def test_writes_all_five_components(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        objectstore_root = tmp_path / "objects"

        _write_components(components_dir, objectstore_root)

        expected = sorted(_COMPONENTS_YAML.keys())
        actual = sorted(p.name for p in components_dir.iterdir())
        assert actual == expected

    def test_objectstore_root_substituted(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        objectstore_root = tmp_path / "objects"

        _write_components(components_dir, objectstore_root)

        content = (components_dir / "objectstore.yaml").read_text()
        assert str(objectstore_root.resolve()) in content
        assert "bindings.localstorage" in content

    def test_statestore_uses_in_memory(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        _write_components(components_dir, tmp_path / "objects")
        assert "state.in-memory" in (components_dir / "statestore.yaml").read_text()

    def test_secretstore_uses_local_env(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        _write_components(components_dir, tmp_path / "objects")
        assert (
            "secretstores.local.env"
            in (components_dir / "secretstore.yaml").read_text()
        )

    def test_objectstore_root_created(self, tmp_path: Path) -> None:
        components_dir = tmp_path / "components"
        objectstore_root = tmp_path / "objects" / "nested"
        assert not objectstore_root.exists()

        _write_components(components_dir, objectstore_root)

        assert objectstore_root.is_dir()


class TestPickFreePort:
    def test_returns_int_in_valid_range(self) -> None:
        port = _pick_free_port()
        assert isinstance(port, int)
        # Ephemeral port range — varies by OS, but always > 1023.
        assert 1024 <= port <= 65535


class TestEmbeddedDaprLifecycle:
    """``embedded_dapr`` must save/restore env vars and clean up its temp dir."""

    @pytest.fixture(autouse=True)
    def _stub_subprocess_and_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Replace binary download, subprocess spawn, and readiness poll
        with no-ops so the context manager runs end-to-end without
        touching the network or spawning a real daprd."""
        fake_binary = tmp_path / "daprd-stub"
        fake_binary.write_text("#!/bin/sh\n")
        fake_binary.chmod(0o755)

        monkeypatch.setattr(
            "application_sdk.dev._dapr._ensure_daprd_binary",
            lambda: fake_binary,
        )

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.wait = AsyncMock(return_value=0)
        fake_proc.terminate = MagicMock()
        fake_proc.kill = MagicMock()

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_proc

        monkeypatch.setattr(
            "asyncio.create_subprocess_exec", _fake_create_subprocess_exec
        )
        monkeypatch.setattr(
            "application_sdk.dev._dapr._wait_for_dapr_ready",
            AsyncMock(return_value=None),
        )

        return fake_proc

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
