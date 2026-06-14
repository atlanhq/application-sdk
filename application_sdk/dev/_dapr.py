"""Embedded Dapr sidecar for local app development.

Auto-downloads the ``daprd`` binary into ``~/.cache/atlan-sdk/dapr/`` on
first use, writes a minimal set of components (in-memory state, env-var
secrets, filesystem object store) to a temp dir, spawns ``daprd``, and
tears it down on context exit.

The goal is to keep local development on the **same Dapr code path** as
production while requiring nothing on the host beyond Python and ``uv``.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import socket
import stat
import tarfile
import tempfile
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from application_sdk.dev._dapr_errors import (
    DaprdBinaryMissingError,
    DaprReadinessTimeoutError,
    UnsupportedArchitectureError,
    UnsupportedOsError,
)

# Single source of truth for the daprd pin lives in ``application_sdk.version``
# (see the ``__dapr_version`` constant). CI workflows grep the same file so
# the literal version string lives in exactly one place.
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.version import __dapr_version as _DAPRD_VERSION

logger = get_logger(__name__)
_DAPRD_RELEASE_BASE = (
    "https://github.com/dapr/dapr/releases/download/v{version}/daprd_{os}_{arch}"
)
_CACHE_DIR = Path.home() / ".cache" / "atlan-sdk" / "dapr" / _DAPRD_VERSION


def _daprd_binary_name() -> str:
    return "daprd.exe" if platform.system().lower() == "windows" else "daprd"


def _daprd_archive_ext() -> str:
    # Windows releases use .zip; Linux/macOS use .tar.gz
    return ".zip" if platform.system().lower() == "windows" else ".tar.gz"


@dataclass(frozen=True)
class EmbeddedDapr:
    """Connection details for the embedded Dapr sidecar."""

    http_port: int
    grpc_port: int
    components_dir: str


def _platform_tuple() -> tuple[str, str]:
    """Return ``(os, arch)`` strings matching Dapr's release asset naming."""
    system = platform.system().lower()  # darwin / linux / windows
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        raise UnsupportedArchitectureError(architecture=platform.machine())
    if system not in ("darwin", "linux", "windows"):
        raise UnsupportedOsError(os_name=platform.system())
    return system, arch


def _download_daprd(target: Path) -> None:
    """Download + extract the ``daprd`` binary into *target*."""
    os_name, arch = _platform_tuple()
    ext = _daprd_archive_ext()
    binary_name = _daprd_binary_name()
    url = (
        _DAPRD_RELEASE_BASE.format(version=_DAPRD_VERSION, os=os_name, arch=arch) + ext
    )
    logger.info("Downloading daprd v%s for %s/%s …", _DAPRD_VERSION, os_name, arch)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Use mkstemp + immediate close so the file handle is released before
    # urlretrieve opens it — NamedTemporaryFile on Windows keeps an exclusive
    # lock that prevents urlretrieve from opening the same path ([WinError 32]).
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=ext)
    os.close(tmp_fd)
    try:
        urllib.request.urlretrieve(url, tmp_name)
        if ext == ".zip":
            import zipfile  # noqa: PLC0415 — Windows-only cold path

            with zipfile.ZipFile(tmp_name) as zf:
                member = next(
                    (m for m in zf.namelist() if m.endswith(binary_name)),
                    None,
                )
                if member is None:
                    raise DaprdBinaryMissingError(archive_url=url, archive_format="zip")
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(tmp_name, "r:gz") as tar:
                member = next(
                    (m for m in tar.getmembers() if m.name.endswith(binary_name)),
                    None,
                )
                if member is None:
                    raise DaprdBinaryMissingError(archive_url=url, archive_format="tar")
                with tar.extractfile(member) as src, target.open("wb") as dst:  # type: ignore[arg-type]
                    shutil.copyfileobj(src, dst)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("daprd cached at %s", target)


def _ensure_daprd_binary() -> Path:
    """Return path to the daprd binary, downloading if absent."""
    binary = _CACHE_DIR / _daprd_binary_name()
    if not binary.exists():
        _download_daprd(binary)
    return binary


def _pick_free_port() -> int:
    """Bind a TCP socket to port 0, read the port, release. Race-free enough."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


DEFAULT_OBJECTSTORE_ROOT = "./local/dapr/objectstore"
DEFAULT_EVENTSTORE_ROOT = "./local/dapr/eventstore"

_COMPONENTS_YAML = {
    "statestore.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.in-memory
  version: v1
  metadata: []
""",
    "secretstore.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: secretstore
spec:
  type: secretstores.local.env
  version: v1
  metadata: []
""",
    "deployment-secret-store.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: deployment-secret-store
spec:
  type: secretstores.local.env
  version: v1
  metadata: []
""",
    "objectstore.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: objectstore
spec:
  type: bindings.localstorage
  version: v1
  metadata:
    - name: rootPath
      value: {objectstore_root}
""",
    "eventstore.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: eventstore
spec:
  type: bindings.localstorage
  version: v1
  metadata:
    - name: rootPath
      value: {eventstore_root}
""",
}


def _write_components(
    components_dir: Path,
    objectstore_root: Path = Path(DEFAULT_OBJECTSTORE_ROOT),
    eventstore_root: Path = Path(DEFAULT_EVENTSTORE_ROOT),
) -> None:
    """Write the auto-generated Dapr component YAMLs into *components_dir*."""
    components_dir.mkdir(parents=True, exist_ok=True)
    objectstore_root.mkdir(parents=True, exist_ok=True)
    eventstore_root.mkdir(parents=True, exist_ok=True)
    for filename, template in _COMPONENTS_YAML.items():
        (components_dir / filename).write_text(
            template.format(
                objectstore_root=str(objectstore_root.resolve()),
                eventstore_root=str(eventstore_root.resolve()),
            )
        )


async def _wait_for_dapr_ready(http_port: int, timeout_s: float = 30.0) -> None:
    """Poll Dapr's metadata endpoint until it responds or *timeout_s* elapses."""
    import httpx  # noqa: PLC0415 — defer the heavy import

    deadline = asyncio.get_event_loop().time() + timeout_s
    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                resp = await client.get(
                    f"http://127.0.0.1:{http_port}/v1.0/metadata", timeout=1.0
                )
                if resp.status_code == 200:
                    return
            # conformance: ignore[E014] Dapr readiness poll; transient connection errors expected
            except Exception:  # noqa: BLE001, S110 — readiness loop
                pass
            await asyncio.sleep(0.25)
    raise DaprReadinessTimeoutError(timeout_seconds=float(timeout_s))


@asynccontextmanager
async def embedded_dapr(
    *,
    app_id: str = "atlan-app",
    objectstore_root: str = DEFAULT_OBJECTSTORE_ROOT,
    eventstore_root: str = DEFAULT_EVENTSTORE_ROOT,
    log_level: str = "warn",
) -> AsyncIterator[EmbeddedDapr]:
    """Boot an embedded ``daprd`` for local app development.

    Components written into a temp dir:

    * ``statestore`` — ``state.in-memory``
    * ``secretstore`` / ``deployment-secret-store`` — ``secretstores.local.env``
    * ``objectstore`` — ``bindings.localstorage`` rooted at *objectstore_root*
    * ``eventstore`` — ``bindings.localstorage`` rooted at *eventstore_root*

    On entry the context manager sets ``DAPR_HTTP_PORT``, ``DAPR_GRPC_PORT``,
    and ``DAPR_COMPONENTS_PATH`` so callers (and any background observability
    flush that races with daprd startup) see the right values. The previous
    values of those env vars are restored on exit.
    """
    binary = _ensure_daprd_binary()
    http_port = _pick_free_port()
    grpc_port = _pick_free_port()
    components_dir = Path(tempfile.mkdtemp(prefix="atlan-dapr-"))
    _write_components(components_dir, Path(objectstore_root), Path(eventstore_root))

    # Set env BEFORE spawning the subprocess so the observability sink's
    # flush cycle (which may fire during the ~3s daprd startup window) finds
    # the auto-generated component YAMLs instead of the default ``./components``.
    _prev_env = {
        k: os.environ.get(k)
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH")
    }
    os.environ["DAPR_HTTP_PORT"] = str(http_port)
    os.environ["DAPR_GRPC_PORT"] = str(grpc_port)
    os.environ["DAPR_COMPONENTS_PATH"] = str(components_dir)

    logger.info("Starting embedded Dapr (http=%d grpc=%d)", http_port, grpc_port)
    proc = await asyncio.create_subprocess_exec(
        str(binary),
        "--app-id",
        app_id,
        "--dapr-http-port",
        str(http_port),
        "--dapr-grpc-port",
        str(grpc_port),
        "--resources-path",
        str(components_dir),
        "--log-level",
        log_level,
        # Disable the metrics + placement endpoints we don't need for local dev.
        "--enable-metrics=false",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await _wait_for_dapr_ready(http_port)
        logger.info("Embedded Dapr ready at http://127.0.0.1:%d", http_port)
        yield EmbeddedDapr(
            http_port=http_port,
            grpc_port=grpc_port,
            components_dir=str(components_dir),
        )
    finally:
        logger.info("Shutting down embedded Dapr")
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        shutil.rmtree(components_dir, ignore_errors=True)
        for _k, _v in _prev_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
