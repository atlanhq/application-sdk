"""The streaming upload and download keep the event loop free per chunk.

``upload_file`` reads its source a part at a time and ``download_file`` writes
each streamed chunk to its staging file. Both used to do that file I/O on the
event loop, so a slow or saturated volume held the loop — and the activity
heartbeat — for a whole multi-MiB chunk. Each test routes the file call through
a :class:`~tests.unit.loop_probe.LoopProbe` and checks which thread ran it and
that the loop ran while it was blocked.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, BinaryIO
from unittest import mock

from application_sdk.storage import _ops_stream
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import _get_bytes, _put, download_file, upload_file
from tests.unit.loop_probe import LoopProbe
from tests.unit.private_stdlib import private_stdlib


class _ProbedFile:
    """A real file whose ``read`` / ``write`` block on the probe first."""

    def __init__(self, fh: BinaryIO, probe: LoopProbe, threads: list[int]) -> None:
        self._fh = fh
        self._probe = probe
        self._threads = threads

    def _blocked(self) -> None:
        self._threads.append(threading.get_ident())
        self._probe.block()

    def read(self, size: int = -1) -> bytes:
        self._blocked()
        return self._fh.read(size)

    def write(self, data: bytes) -> int:
        self._blocked()
        return self._fh.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fh, name)

    def __enter__(self) -> _ProbedFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self._fh.close()


async def test_download_writes_each_chunk_off_the_loop(tmp_path: Path) -> None:
    probe = LoopProbe()
    threads: list[int] = []
    store = create_memory_store()
    content = b"x" * (3 * 1024)
    await _put("k.bin", content, store, normalize=False)
    dest = tmp_path / "k.bin"

    with private_stdlib(_ops_stream, "os") as local_os:
        real_fdopen = local_os.fdopen
        with mock.patch.object(
            local_os,
            "fdopen",
            lambda fd, mode: _ProbedFile(real_fdopen(fd, mode), probe, threads),
        ):
            with probe.bound():
                await download_file("k.bin", dest, store, normalize=False, verify=False)

    assert dest.read_bytes() == content
    assert threads, "the staging file was never written"
    assert threading.get_ident() not in threads, "a chunk write ran on the loop"
    probe.assert_loop_stayed_free("download_file chunk writes")


async def test_upload_reads_each_part_off_the_loop(tmp_path: Path) -> None:
    probe = LoopProbe()
    threads: list[int] = []
    store = create_memory_store()
    content = b"y" * (3 * 1024)
    src = tmp_path / "src.bin"
    src.write_bytes(content)
    real_open = Path.open

    def _open(self: Path, *args: Any, **kwargs: Any) -> Any:
        fh = real_open(self, *args, **kwargs)
        return _ProbedFile(fh, probe, threads) if self == src else fh

    with mock.patch.object(Path, "open", _open):
        with probe.bound():
            await upload_file(
                "src.bin",
                src,
                store,
                normalize=False,
                verify=False,
                retain_local_copy=True,
            )

    assert await _get_bytes("src.bin", store, normalize=False) == content
    assert threads, "the source file was never read"
    assert threading.get_ident() not in threads, "a part read ran on the loop"
    probe.assert_loop_stayed_free("upload_file part reads")
