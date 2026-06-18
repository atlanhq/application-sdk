"""Local-file writers for lineage-observability artifacts.

Severs the argo framework's dependency on
``marketplace_scripts.utils.chunked_output_handler``: :class:`ChunkedOutputHandler`
is reproduced here (same constructor / ``write`` / ``close`` contract) so the
verbatim tracker and its regression suite run unchanged inside the SDK.

These writers emit to the LOCAL filesystem (the activity's working dir). Uploading
partials to the object store and the cross-activity reduce live in the distributed
layer (added in a later PR); this module is the local, synchronous substrate they
build on.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional, TextIO

import orjson

logger = logging.getLogger(__name__)


class ChunkedOutputHandler:
    """Streaming JSONL writer with optional file chunking.

    One JSON object per line. With a falsy ``chunk_size`` all records go to a
    single ``{prefix}.json``; otherwise files roll over every ``chunk_size``
    records as ``{prefix}-{n}.json``. Ported from the argo framework so the
    per-asset output is byte-compatible with the existing Tableau pipeline.
    """

    def __init__(
        self,
        output_file_prefix: Optional[str] = None,
        chunk_size: Optional[int] = None,
    ) -> None:
        if output_file_prefix:
            directory = os.path.dirname(output_file_prefix)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self.output_file_prefix = output_file_prefix
        self.output_file: Optional[TextIO] = None
        self.chunk_size = chunk_size
        self.entity_count = 0
        self.current_chunk_number: Optional[int] = None

    def generate_file_ref(self) -> None:
        self.entity_count += 1

        if not self.chunk_size:
            self.current_chunk_number = 0
            if not self.output_file:
                self.output_file = open(f"{self.output_file_prefix}.json", "w")
            return

        suffix_number = int(self.entity_count / self.chunk_size)
        if suffix_number != self.current_chunk_number:
            self.close()
            file_path = f"{self.output_file_prefix}-{suffix_number}.json"
            directory = os.path.dirname(file_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self.output_file = open(file_path, "w")
            self.current_chunk_number = suffix_number

    def write(self, data: Dict[str, Any]) -> None:
        if not self.output_file_prefix:
            logger.debug(
                "ChunkedOutputHandler has no output_file_prefix; record not written"
            )
            return

        self.generate_file_ref()
        if (
            self.output_file is None
        ):  # generate_file_ref always opens it; satisfy type narrowing
            return
        try:
            self.output_file.write(
                orjson.dumps(data, option=orjson.OPT_APPEND_NEWLINE).decode("utf-8")
            )
        except TypeError as exc:  # non-orjson-serialisable values
            logger.warning(
                "orjson serialisation failed (%s); falling back to json.dumps", exc
            )
            self.output_file.write(json.dumps(data) + "\n")

    def write_str(self, data: str) -> None:
        if not self.output_file_prefix:
            logger.debug(
                "ChunkedOutputHandler has no output_file_prefix; string not written"
            )
            return
        if not data.endswith("\n"):
            data += "\n"
        self.generate_file_ref()
        if (
            self.output_file is None
        ):  # generate_file_ref always opens it; satisfy type narrowing
            return
        self.output_file.write(data)

    def output_files_generated(self) -> None:
        if not self.output_file_prefix:
            return
        directory = os.path.dirname(self.output_file_prefix)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(f"{self.output_file_prefix}-gen.txt", "w") as handle:
            count = (
                self.current_chunk_number + 1
                if self.current_chunk_number is not None
                else -1
            )
            handle.write(str(count))

    def close(self) -> None:
        self.output_files_generated()
        if not self.output_file:
            return
        self.output_file.close()
        self.output_file = None


def write_coverage_json(coverage_output: Dict[str, Any], output_path: str) -> None:
    """Write a coverage summary dict to *output_path* as pretty JSON."""
    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(output_path, "w") as handle:
        json.dump(coverage_output, handle, indent=2)


def create_asset_details_handler(
    output_file_prefix: str,
    suffix: str = "lineage-asset-details",
    chunk_size: int = 0,
) -> ChunkedOutputHandler:
    """Create a :class:`ChunkedOutputHandler` for per-asset JSONL output.

    Produces ``{output_file_prefix}{suffix}.json`` (single file when
    ``chunk_size`` is 0).
    """
    return ChunkedOutputHandler(
        output_file_prefix=f"{output_file_prefix}{suffix}",
        chunk_size=chunk_size,
    )


__all__ = [
    "ChunkedOutputHandler",
    "write_coverage_json",
    "create_asset_details_handler",
]
