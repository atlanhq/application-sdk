"""Unit tests for AppDeployer."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.testing.e2e.config import AppConfig
from application_sdk.testing.e2e.deployer import (
    AppDeployer,
    DeploymentError,
    _parse_image,
)

# ---------------------------------------------------------------------------
# _parse_image
# ---------------------------------------------------------------------------


def test_parse_image_full_registry():
    result = _parse_image("ghcr.io/atlanhq/my-app:v1.0.0")
    assert result["image.registry"] == "ghcr.io"
    assert result["image.repository"] == "atlanhq/my-app"
    assert result["image.tag"] == "v1.0.0"


def test_parse_image_localhost():
    result = _parse_image("localhost/my-app:e2e")
    assert result["image.registry"] == "localhost"
    assert result["image.repository"] == "my-app"
    assert result["image.tag"] == "e2e"


def test_parse_image_no_registry():
    result = _parse_image("my-app:latest")
    assert "image.registry" not in result
    assert result["image.repository"] == "my-app"
    assert result["image.tag"] == "latest"


def test_parse_image_no_tag():
    result = _parse_image("ghcr.io/atlanhq/my-app")
    assert result["image.registry"] == "ghcr.io"
    assert result["image.repository"] == "atlanhq/my-app"
    assert result["image.tag"] == "latest"


def test_parse_image_registry_with_port():
    result = _parse_image("localhost:5000/my-app:test")
    assert result["image.registry"] == "localhost:5000"
    assert result["image.repository"] == "my-app"
    assert result["image.tag"] == "test"


# ---------------------------------------------------------------------------
# AppDeployer
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(
        app_name="test-app",
        app_module="test_app.main:App",
        namespace="app-test",
        image="ghcr.io/atlanhq/test-app:v1.0.0",
        timeout=120,
    )


@pytest.fixture
def deployer(config: AppConfig) -> AppDeployer:
    return AppDeployer(config, chart_path=Path("/fake/helm/chart"))


def _make_proc(
    returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


@pytest.mark.asyncio
async def test_deploy_calls_helm_upgrade(deployer: AppDeployer):
    proc = _make_proc()
    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        await deployer.deploy()

    assert mock_exec.called
    args = mock_exec.call_args[0]
    assert args[0] == "helm"
    assert "upgrade" in args
    assert "--install" in args
    assert "--wait" in args
    assert "--timeout=120s" in args
    assert "test-app" in args
    # Namespace
    ns_idx = list(args).index("--namespace")
    assert args[ns_idx + 1] == "app-test"


@pytest.mark.asyncio
async def test_deploy_raises_on_failure(deployer: AppDeployer):
    proc = _make_proc(returncode=1, stderr=b"helm error")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(DeploymentError, match="helm error"):
            await deployer.deploy()


@pytest.mark.asyncio
async def test_deploy_includes_set_values(deployer: AppDeployer):
    proc = _make_proc()
    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        await deployer.deploy()

    args = list(mock_exec.call_args[0])
    set_pairs = {}
    for i, arg in enumerate(args):
        if arg == "--set":
            k, v = args[i + 1].split("=", 1)
            set_pairs[k] = v

    assert set_pairs["appName"] == "test-app"
    assert set_pairs["appModule"] == "test_app.main:App"
    assert set_pairs["image.registry"] == "ghcr.io"
    assert set_pairs["image.repository"] == "atlanhq/test-app"
    assert set_pairs["image.tag"] == "v1.0.0"


@pytest.mark.asyncio
async def test_helm_values_override_image(config: AppConfig):
    config.helm_values = {"image.tag": "custom-tag"}
    deployer = AppDeployer(config, chart_path=Path("/fake/helm/chart"))

    proc = _make_proc()
    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        await deployer.deploy()

    args = list(mock_exec.call_args[0])
    set_pairs = {}
    for i, arg in enumerate(args):
        if arg == "--set":
            k, v = args[i + 1].split("=", 1)
            set_pairs[k] = v

    assert set_pairs["image.tag"] == "custom-tag"


@pytest.mark.asyncio
async def test_undeploy_calls_helm_uninstall(deployer: AppDeployer):
    procs = [_make_proc(), _make_proc()]
    call_count = 0

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        p = procs[call_count]
        call_count += 1
        return p

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec) as mock_exec:
        await deployer.undeploy()

    first_call_args = mock_exec.call_args_list[0][0]
    assert first_call_args[0] == "helm"
    assert "uninstall" in first_call_args


@pytest.mark.asyncio
async def test_undeploy_ignores_helm_failure(deployer: AppDeployer):
    procs = [_make_proc(returncode=1, stderr=b"not found"), _make_proc()]
    call_count = 0

    async def fake_exec(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        p = procs[call_count]
        call_count += 1
        return p

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        # Should not raise even when helm uninstall fails
        await deployer.undeploy()


@pytest.mark.asyncio
async def test_is_ready_true(deployer: AppDeployer):
    proc = _make_proc(stdout=b"true true")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await deployer.is_ready() is True


@pytest.mark.asyncio
async def test_is_ready_false_when_not_all_ready(deployer: AppDeployer):
    proc = _make_proc(stdout=b"true false")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await deployer.is_ready() is False


@pytest.mark.asyncio
async def test_is_ready_false_when_no_pods(deployer: AppDeployer):
    proc = _make_proc(stdout=b"")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await deployer.is_ready() is False
