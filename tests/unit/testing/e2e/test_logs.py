"""Unit tests for LogCollector."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.testing.e2e.logs import LogCollector


def _make_proc(stdout: bytes = b"log output", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    return tmp_path / "test-logs"


@pytest.mark.asyncio
async def test_collect_creates_output_dir(output_dir: Path):
    with (
        patch(
            "application_sdk.testing.e2e.logs.get_pods", new=AsyncMock(return_value=[])
        ),
        patch("asyncio.create_subprocess_exec", return_value=_make_proc()),
    ):
        collector = LogCollector("test-ns", output_dir)
        await collector.collect()

    assert output_dir.is_dir()


@pytest.mark.asyncio
async def test_collect_writes_pods_wide(output_dir: Path):
    with (
        patch(
            "application_sdk.testing.e2e.logs.get_pods", new=AsyncMock(return_value=[])
        ),
        patch(
            "asyncio.create_subprocess_exec",
            return_value=_make_proc(
                stdout=b"NAME   READY   STATUS\npod-1  1/1  Running"
            ),
        ),
    ):
        collector = LogCollector("test-ns", output_dir)
        await collector.collect()

    pods_wide = output_dir / "pods-wide.txt"
    assert pods_wide.exists()
    assert b"pod-1" in pods_wide.read_bytes()


@pytest.mark.asyncio
async def test_collect_writes_container_logs(output_dir: Path):
    pod = {
        "metadata": {"name": "my-pod"},
        "status": {"containerStatuses": [{"name": "handler", "restartCount": 0}]},
    }

    procs: list[MagicMock] = []

    def make_proc(*args: object, **kwargs: object) -> MagicMock:
        p = _make_proc(stdout=b"handler log line")
        procs.append(p)
        return p

    with (
        patch(
            "application_sdk.testing.e2e.logs.get_pods",
            new=AsyncMock(return_value=[pod]),
        ),
        patch("asyncio.create_subprocess_exec", side_effect=make_proc),
    ):
        collector = LogCollector("test-ns", output_dir)
        await collector.collect()

    log_file = output_dir / "handler-my-pod.log"
    assert log_file.exists()
    assert b"handler log line" in log_file.read_bytes()


@pytest.mark.asyncio
async def test_collect_writes_previous_logs_on_restart(output_dir: Path):
    pod = {
        "metadata": {"name": "crash-pod"},
        "status": {"containerStatuses": [{"name": "worker", "restartCount": 2}]},
    }

    with (
        patch(
            "application_sdk.testing.e2e.logs.get_pods",
            new=AsyncMock(return_value=[pod]),
        ),
        patch(
            "asyncio.create_subprocess_exec",
            return_value=_make_proc(stdout=b"crash logs"),
        ),
    ):
        collector = LogCollector("test-ns", output_dir)
        await collector.collect()

    previous_log = output_dir / "worker-crash-pod-previous.log"
    assert previous_log.exists()


@pytest.mark.asyncio
async def test_collect_events_writes_events_file(output_dir: Path):
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_make_proc(stdout=b"Warning BackOff  ..."),
    ):
        collector = LogCollector("test-ns", output_dir)
        await collector.collect_events()

    events_file = output_dir / "events.txt"
    assert events_file.exists()
    assert b"BackOff" in events_file.read_bytes()


@pytest.mark.asyncio
async def test_collect_never_raises_on_subprocess_error(output_dir: Path):
    """LogCollector is best-effort — subprocess failures must not propagate."""
    with (
        patch(
            "application_sdk.testing.e2e.logs.get_pods", new=AsyncMock(return_value=[])
        ),
        patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("kubectl not found"),
        ),
    ):
        collector = LogCollector("test-ns", output_dir)
        # Should not raise
        await collector.collect()
        await collector.collect_events()
