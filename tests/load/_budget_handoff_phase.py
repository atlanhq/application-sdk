"""One phase of the four-directory-ref hand-off, run in its own process.

Driven by ``test_transfer_budget_load.py``. Each phase runs in a fresh
interpreter because peak RSS (``ru_maxrss``) only ever rises, and because the
allocator keeps buffers a phase has freed: measured in one process, the
download phase would sit on top of the upload phase's retained memory and be
charged for it.

Usage: ``python _budget_handoff_phase.py {persist|materialize} ROOT FILE_BYTES``.
Prints one line: the phase's peak-RSS growth in bytes.
"""

from __future__ import annotations

import asyncio
import resource
import shutil
import sys
from pathlib import Path

from conftest import write_source_file

from application_sdk.contracts.base import Input
from application_sdk.contracts.types import FileReference
from application_sdk.storage.factory import create_local_store
from application_sdk.storage.file_ref_sync import (
    materialize_file_refs,
    persist_file_refs,
)

_FILES_PER_REF = 4


class FourRefs(Input):
    a: FileReference
    b: FileReference
    c: FileReference
    d: FileReference


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


async def _persist(root: Path, file_bytes: int) -> int:
    store = create_local_store(root / "objectstore")
    refs: dict[str, FileReference] = {}
    for name in "abcd":
        directory = root / name
        directory.mkdir(parents=True)
        for i in range(_FILES_PER_REF):
            write_source_file(directory / f"part-{i}.bin", file_bytes)
        refs[name] = FileReference.from_local(str(directory))
    before = _peak_rss_bytes()
    persisted = await persist_file_refs(store, FourRefs(**refs))
    growth = _peak_rss_bytes() - before
    for name in "abcd":
        shutil.rmtree(root / name)  # the next phase is a different pod
    (root / "refs.json").write_text(persisted.model_dump_json())
    return growth


async def _materialize(root: Path) -> int:
    store = create_local_store(root / "objectstore")
    persisted = FourRefs.model_validate_json((root / "refs.json").read_text())
    before = _peak_rss_bytes()
    await materialize_file_refs(store, persisted)
    return _peak_rss_bytes() - before


def main() -> None:
    phase, root, file_bytes = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
    if phase == "persist":
        growth = asyncio.run(_persist(root, file_bytes))
    else:
        growth = asyncio.run(_materialize(root))
    print(growth)  # noqa: T201 — the parent test reads this line


if __name__ == "__main__":
    main()
