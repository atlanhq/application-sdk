"""Workflow-config persistence — pluggable store with a local-filesystem default.

Per-app workflow config is persisted under a fixed object-store key convention.
The default :class:`LocalFileConfigStore` is dependency-free, so the serving
image needs no obstore / boto3 / Dapr; a production deployment can inject an
object-store-backed :class:`ConfigStore` instead.

Key convention:
    persistent-artifacts/apps/{APPLICATION_NAME}/{config_type}/{config_id}/config.json

The 503-vs-404 distinction is driven by the store *reference* being ``None``
(no store configured → 503), not by the store's own return values (present but
missing key → 404). ``server_sdk.server`` preserves that by holding an optional
``ConfigStore`` and mapping ``None``/``False`` accordingly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

try:  # orjson: fast, stable JSON serialization for stored config files.
    import orjson

    def _json_dumps(obj: Any) -> bytes:
        return orjson.dumps(obj)

    def _json_loads(data: bytes) -> Any:
        return orjson.loads(data)

except ModuleNotFoundError:  # pragma: no cover - orjson is a declared core dep
    import json

    def _json_dumps(obj: Any) -> bytes:
        return json.dumps(obj).encode()

    def _json_loads(data: bytes) -> Any:
        return json.loads(data)


# Character class enforced on config_id / config_type (drives 422 on mismatch).
CONFIG_KEY_PATTERN = r"^[a-zA-Z0-9_\-]{1,128}$"
_CONFIG_KEY_RE = re.compile(CONFIG_KEY_PATTERN)


def application_name() -> str:
    """The app whose config tree we address — ``ATLAN_APPLICATION_NAME`` or ``default``."""
    return os.getenv("ATLAN_APPLICATION_NAME", "default")


def config_objectstore_key(
    config_id: str, config_type: str = "workflows", app_name: str | None = None
) -> str:
    """Build the object-store key, matching the v2 SDK statestore path convention.

    ``app_name`` scopes the key to one app's config tree. It must be passed in
    multi-app hosts (the common API server serves many apps from one process, so
    the ``ATLAN_APPLICATION_NAME`` env fallback would collide every hosted app
    into a single tree). ``None`` keeps the standalone behavior: the env var —
    which per-app charts set to the app's own name — decides the tree.
    """
    if not _CONFIG_KEY_RE.match(config_id):
        raise ValueError(f"Invalid config_id: {config_id!r}")
    if not _CONFIG_KEY_RE.match(config_type):
        raise ValueError(f"Invalid config_type: {config_type!r}")
    owner = app_name or application_name()
    if not _CONFIG_KEY_RE.match(owner):
        raise ValueError(f"Invalid app_name: {owner!r}")
    return f"persistent-artifacts/apps/{owner}/{config_type}/{config_id}/config.json"


@runtime_checkable
class ConfigStore(Protocol):
    """Minimal async config backend."""

    async def load(self, key: str) -> dict[str, Any] | None:
        """Return the parsed JSON dict, or ``None`` if absent/unreadable/unparseable."""
        ...

    async def save(self, key: str, body: dict[str, Any]) -> None:
        """Serialize and persist ``body`` at ``key`` (atomically where possible)."""
        ...


class LocalFileConfigStore:
    """Filesystem-backed :class:`ConfigStore` — no obstore/S3/Dapr.

    Mirrors the object-store layout on disk: the key's POSIX path becomes nested
    directories under ``root`` ending in ``config.json``. ``load`` swallows all
    read/parse errors and returns ``None`` (identical to the obstore loader), so
    the endpoint's 404 path is preserved.
    """

    def __init__(self, root: str | Path = "./local/config") -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        parts = PurePosixPath(key).parts
        resolved_root = self._root.resolve()
        p = (self._root / Path(*parts)).resolve()
        if not p.is_relative_to(resolved_root):
            raise ValueError(f"Path traversal detected in key: {key!r}")
        return p

    async def load(self, key: str) -> dict[str, Any] | None:
        try:
            return _json_loads(self._path(key).read_bytes())
        except Exception:
            return None

    async def save(self, key: str, body: dict[str, Any]) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(_json_dumps(body))
        os.replace(tmp, p)  # atomic within the same filesystem
