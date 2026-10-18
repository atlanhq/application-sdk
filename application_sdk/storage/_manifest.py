"""Directory manifest for durable directory ``FileReference`` hand-offs (FND-3762).

A directory ref used to be a prefix plus a ``file_count``, and every consumer
re-listed the prefix and trusted the listing. A listing cannot tell a
partial upload from a small one, and it picks up retry leftovers and late
writes from a timed-out earlier attempt as if they were data.

The manifest names exactly which objects the hand-off covers. The producer
— the upload engine behind both persist and ``App.upload()`` — writes it as
the **last** object of a directory upload, so it exists only
after every data object landed. A consumer that has one downloads exactly its
entries and checks each against the manifest's digest. It never lists.

Location: under the tier's ``file_refs`` namespace
(``{base}/file_refs/{uuid}.sdk-manifest``), never inside the data prefix. An
older SDK still lists the prefix, and a manifest inside it would be read as a
data file. The one exception is ``App.upload()``'s default prefix, the run
root, which contains the ``file_refs`` namespace; the cross-repo contract
records why that is safe. The suffix is deliberately not ``.json``: publish converters and
apps glob run prefixes with ``**/*.json`` and would parse the manifest as asset
records — the same reason ``CurrentStateStore`` names its manifest
``.sdk-manifest``. The key travels on the ref as ``FileReference.manifest_path``, so
consumers never derive it from the prefix. The wire format is a cross-repo
contract; see ``docs/standards/cross-repo-contracts.md``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: Suffix of a manifest key. Never ``.json``: see the module docstring.
MANIFEST_SUFFIX = ".sdk-manifest"


class ManifestEntry(BaseModel, frozen=True):
    """One data object a directory ref covers."""

    path: str = Field(min_length=1)
    """Key relative to the ref's ``storage_path`` prefix, ``/``-separated."""
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    """Hex SHA-256 of the object's bytes, as computed at upload."""
    size_bytes: int = Field(ge=0)


class DirectoryManifest(BaseModel, frozen=True):
    """The exact file set of a durable directory ``FileReference``."""

    version: Literal[1] = 1
    files: list[ManifestEntry]
