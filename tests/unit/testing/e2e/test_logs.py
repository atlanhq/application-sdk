"""Unit tests for LogCollector.

The pod listing and the container-log reads go through the typed cluster reader
now (FND-241); ``describe``, ``get pods -o wide`` and ``get events`` still shell
out. The written file layout is what these tests pin, because that is the part a
reviewer of a red CI leg actually depends on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.testing.e2e.logs import LogCollector
from application_sdk.testing.harness.cluster import PodPhase, PodState


def _make_proc(stdout: bytes = b"log output", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


def _pod(name: str, **containers: int) -> PodState:
    return PodState(
        name=name,
        namespace="test-ns",
        phase=PodPhase.RUNNING,
        ready=True,
        restarts=sum(containers.values()),
        containers=containers or None,
    )


class _FakeReader:
    """Just the two verbs LogCollector uses, plus a record of the calls."""

    def __init__(
        self,
        pods: list[PodState] | BaseException,
        log_text: str = "log line",
        kube_context: str | None = None,
    ) -> None:
        self._pods = pods
        self._log_text = log_text
        self.kube_context = kube_context
        self.log_calls: list[dict[str, Any]] = []

    async def pods(self, namespace: str, selector: str = "") -> list[PodState]:
        if isinstance(self._pods, BaseException):
            raise self._pods
        return self._pods

    async def container_log(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        tail_lines: int | None = -1,
        since: object = None,
    ) -> str:
        self.log_calls.append(
            {
                "pod": pod,
                "container": container,
                "previous": previous,
                "tail_lines": tail_lines,
            }
        )
        return self._log_text


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    return tmp_path / "test-logs"


@pytest.mark.asyncio
async def test_collect_creates_output_dir(output_dir: Path):
    with patch("asyncio.create_subprocess_exec", return_value=_make_proc()):
        collector = LogCollector("test-ns", output_dir, reader=_FakeReader([]))
        await collector.collect()

    assert output_dir.is_dir()


@pytest.mark.asyncio
async def test_collect_writes_pods_wide(output_dir: Path):
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_make_proc(stdout=b"NAME   READY   STATUS\npod-1  1/1  Running"),
    ):
        collector = LogCollector("test-ns", output_dir, reader=_FakeReader([]))
        await collector.collect()

    pods_wide = output_dir / "pods-wide.txt"
    assert pods_wide.exists()
    assert b"pod-1" in pods_wide.read_bytes()


@pytest.mark.asyncio
async def test_collect_writes_container_logs(output_dir: Path):
    reader = _FakeReader([_pod("my-pod", handler=0)], log_text="handler log line")

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc()):
        collector = LogCollector("test-ns", output_dir, reader=reader)
        await collector.collect()

    log_file = output_dir / "handler-my-pod.log"
    assert log_file.exists()
    assert "handler log line" in log_file.read_text(encoding="utf-8")
    assert reader.log_calls == [
        {
            "pod": "my-pod",
            "container": "handler",
            "previous": False,
            "tail_lines": 10_000,
        }
    ]


@pytest.mark.asyncio
async def test_collect_writes_previous_logs_on_restart(output_dir: Path):
    """A crash loop's actual cause is in the *previous* container's output."""
    reader = _FakeReader([_pod("crash-pod", worker=2)], log_text="crash logs")

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc()):
        collector = LogCollector("test-ns", output_dir, reader=reader)
        await collector.collect()

    assert (output_dir / "worker-crash-pod-previous.log").exists()
    assert [call["previous"] for call in reader.log_calls] == [False, True]


@pytest.mark.asyncio
async def test_collect_describes_every_pod(output_dir: Path):
    """``describe`` stays ``kubectl``: it is a formatter, not an endpoint."""
    commands: list[tuple[str, ...]] = []

    def _record(*args: str, **_kwargs: object) -> MagicMock:
        commands.append(args)
        return _make_proc()

    with patch("asyncio.create_subprocess_exec", side_effect=_record):
        collector = LogCollector(
            "test-ns", output_dir, reader=_FakeReader([_pod("my-pod", handler=0)])
        )
        await collector.collect()

    assert (output_dir / "my-pod-describe.txt").exists()
    assert ("kubectl", "describe", "pod", "my-pod", "-n", "test-ns") in commands


@pytest.mark.asyncio
async def test_collect_events_writes_events_file(output_dir: Path):
    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_make_proc(stdout=b"Warning BackOff  ..."),
    ):
        collector = LogCollector("test-ns", output_dir, reader=_FakeReader([]))
        await collector.collect_events()

    events_file = output_dir / "events.txt"
    assert events_file.exists()
    assert b"BackOff" in events_file.read_bytes()


@pytest.mark.asyncio
async def test_collect_never_raises_on_subprocess_error(output_dir: Path):
    """LogCollector is best-effort — subprocess failures must not propagate."""
    with patch(
        "asyncio.create_subprocess_exec", side_effect=OSError("kubectl not found")
    ):
        collector = LogCollector("test-ns", output_dir, reader=_FakeReader([]))
        # Should not raise
        await collector.collect()
        await collector.collect_events()


@pytest.mark.asyncio
async def test_an_unreadable_pod_listing_still_collects_the_namespace_artefacts(
    output_dir: Path,
):
    """The one place fail-open is right: an evidence dump is never graded.

    A collector that raised here would turn a diagnosable failure into an
    undiagnosable one — and the namespace-wide artefacts often explain why the
    listing itself failed.
    """
    reader = _FakeReader(RuntimeError("cluster unreachable"))

    with patch(
        "asyncio.create_subprocess_exec", return_value=_make_proc(stdout=b"wide output")
    ):
        collector = LogCollector("test-ns", output_dir, reader=reader)
        await collector.collect()

    assert (output_dir / "pods-wide.txt").exists()
    assert reader.log_calls == []


@pytest.mark.asyncio
async def test_an_unreadable_container_log_does_not_stop_the_rest(output_dir: Path):
    reader = _FakeReader([_pod("my-pod", handler=0)])
    reader.container_log = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("forbidden")
    )

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc()):
        collector = LogCollector("test-ns", output_dir, reader=reader)
        await collector.collect()

    assert not (output_dir / "handler-my-pod.log").exists()
    assert (output_dir / "pods-wide.txt").exists()


@pytest.mark.asyncio
async def test_an_uncreatable_output_dir_stops_before_reading_anything(
    tmp_path: Path,
):
    reader = _FakeReader([_pod("my-pod", handler=0)])
    collector = LogCollector("test-ns", tmp_path / "logs", reader=reader)

    with patch.object(
        Path, "mkdir", side_effect=PermissionError("read-only filesystem")
    ):
        await collector.collect()
        await collector.collect_events()

    assert reader.log_calls == []


@pytest.mark.asyncio
async def test_container_logs_are_written_as_utf8_not_as_the_locale_encoding(
    output_dir: Path,
):
    """The locale must never decide how a container's output is written.

    ``Path.write_text`` with no ``encoding`` uses the locale's — cp1252 on
    Windows, which cannot represent most of what a container logs, so a single
    non-ASCII character raises and the whole evidence file is lost. Asserted on
    the kwarg rather than on the round trip because the round trip passes either
    way on a UTF-8 platform: this bug is invisible to Linux CI, which is exactly
    why it needs pinning rather than observing.
    """
    # An arrow, not an accent: cp1252 encodes é and — quite happily, so a test
    # written with those would pass against the bug it claims to pin.
    log_text = "extract → transform: 100 rows"
    with pytest.raises(UnicodeEncodeError):
        log_text.encode("cp1252")

    reader = _FakeReader([_pod("my-pod", handler=0)], log_text=log_text)
    written: list[dict[str, Any]] = []
    real_write_text = Path.write_text

    def _record(self: Path, data: str, **kwargs: Any) -> int:
        if self.suffix == ".log":
            written.append(kwargs)
        return real_write_text(self, data, **kwargs)

    with (
        patch("asyncio.create_subprocess_exec", return_value=_make_proc()),
        patch.object(Path, "write_text", _record),
    ):
        collector = LogCollector("test-ns", output_dir, reader=reader)
        await collector.collect()

    assert written == [{"encoding": "utf-8", "errors": "replace"}]
    assert (output_dir / "handler-my-pod.log").read_text(encoding="utf-8") == log_text


@pytest.mark.asyncio
async def test_the_kubectl_artefacts_pin_the_readers_context(output_dir: Path):
    """Evidence about a different cluster is worse than no evidence.

    The reads go through the typed client and honour `kube_context`; `describe`,
    `get pods -o wide` and `get events` still shell out, and `kubectl` without
    `--context` follows whichever context the kubeconfig marks current. That
    mismatch produces a bundle that is confidently about somewhere else, with
    every command exiting 0.
    """
    commands: list[tuple[str, ...]] = []

    def _record(*args: str, **_kwargs: object) -> MagicMock:
        commands.append(args)
        return _make_proc()

    reader = _FakeReader([_pod("my-pod", handler=0)], kube_context="e2e-gcp")

    with patch("asyncio.create_subprocess_exec", side_effect=_record):
        collector = LogCollector("test-ns", output_dir, reader=reader)
        await collector.collect()
        await collector.collect_events()

    assert commands, "no kubectl artefacts were collected"
    for argv in commands:
        assert argv[0] == "kubectl"
        assert "--context" in argv, argv
        assert argv[argv.index("--context") + 1] == "e2e-gcp"


@pytest.mark.asyncio
async def test_no_context_named_leaves_the_kubectl_argv_alone(output_dir: Path):
    commands: list[tuple[str, ...]] = []

    def _record(*args: str, **_kwargs: object) -> MagicMock:
        commands.append(args)
        return _make_proc()

    with patch("asyncio.create_subprocess_exec", side_effect=_record):
        collector = LogCollector(
            "test-ns", output_dir, reader=_FakeReader([_pod("my-pod", handler=0)])
        )
        await collector.collect()

    assert commands
    assert all("--context" not in argv for argv in commands)


@pytest.mark.asyncio
async def test_the_default_reader_is_the_typed_one():
    """No injected reader means the typed backend, not a ``kubectl`` shell-out."""
    from application_sdk.testing.harness.cluster import KubernetesReader

    assert isinstance(LogCollector("ns", Path("/tmp")).reader, KubernetesReader)  # noqa: S108
