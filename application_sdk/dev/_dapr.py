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
import contextlib
import http.client
import os
import platform
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from application_sdk.dev._dapr_errors import (
    DaprComponentsConfigError,
    DaprComponentsDirNotFoundError,
    DaprdBinaryMissingError,
    DaprdDownloadIncompleteError,
    DaprdExitedError,
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


# A stalled GitHub release download must fail, not hang the caller; a 5xx or a
# dropped connection is worth another attempt before giving up.
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_BACKOFF_SECONDS = 2.0

# Failures a fresh download can fix: the network ones, plus an archive that came
# down short (the archive readers raise these on a truncated stream).
_RETRYABLE_DOWNLOAD_ERRORS: tuple[type[BaseException], ...] = (
    urllib.error.URLError,
    http.client.IncompleteRead,
    ConnectionError,
    TimeoutError,
    EOFError,
    tarfile.ReadError,
    zipfile.BadZipFile,
    zlib.error,
    DaprdDownloadIncompleteError,
)


def _is_retryable_download_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        # 4xx (a missing asset for this version/platform) will not change on retry.
        return exc.code >= 500 or exc.code == 429
    return isinstance(exc, _RETRYABLE_DOWNLOAD_ERRORS)


def _fetch_archive(url: str, dest: Path) -> None:
    """Stream *url* into *dest*, bounded by a socket timeout."""
    with (
        urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as resp,
        dest.open("wb") as out,
    ):
        shutil.copyfileobj(resp, out)


def _extract_daprd(archive: Path, url: str, dest: Path) -> None:
    """Extract the ``daprd`` entry of *archive* into *dest*, checking its size."""
    binary_name = _daprd_binary_name()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            info = next(
                (i for i in zf.infolist() if i.filename.endswith(binary_name)), None
            )
            if info is None:
                raise DaprdBinaryMissingError(archive_url=url, archive_format="zip")
            expected = info.file_size
            with zf.open(info) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive, "r:gz") as tar:
            member = next(
                (m for m in tar.getmembers() if m.name.endswith(binary_name)), None
            )
            if member is None:
                raise DaprdBinaryMissingError(archive_url=url, archive_format="tar")
            expected = member.size
            with tar.extractfile(member) as src, dest.open("wb") as dst:  # type: ignore[union-attr]
                shutil.copyfileobj(src, dst)
    actual = dest.stat().st_size
    if actual != expected:
        raise DaprdDownloadIncompleteError(expected_bytes=expected, actual_bytes=actual)


def _download_daprd(target: Path) -> None:
    """Download + extract the ``daprd`` binary into *target*, atomically.

    Everything is written beside *target* under a per-process temp name and
    only ``os.replace``-d into place once the extracted size matches the
    archive entry, so an interrupted download can never leave a truncated
    binary at *target* — which ``_ensure_daprd_binary`` would otherwise accept
    on every later run.
    """
    os_name, arch = _platform_tuple()
    ext = _daprd_archive_ext()
    url = (
        _DAPRD_RELEASE_BASE.format(version=_DAPRD_VERSION, os=os_name, arch=arch) + ext
    )
    logger.info("Downloading daprd v%s for %s/%s …", _DAPRD_VERSION, os_name, arch)
    target.parent.mkdir(parents=True, exist_ok=True)
    archive = target.with_name(f"{target.name}.{os.getpid()}.download{ext}")
    staged = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            try:
                _fetch_archive(url, archive)
                _extract_daprd(archive, url, staged)
                break
            except _RETRYABLE_DOWNLOAD_ERRORS as exc:
                # HTTPError is a URLError, so a 4xx lands here too and is
                # re-raised at once by the classifier.
                if attempt == _DOWNLOAD_ATTEMPTS or not _is_retryable_download_error(
                    exc
                ):
                    raise
                logger.warning(
                    "daprd download attempt %d/%d failed (%s); retrying",
                    attempt,
                    _DOWNLOAD_ATTEMPTS,
                    type(exc).__name__,
                    exc_info=True,
                )
                time.sleep(_DOWNLOAD_BACKOFF_SECONDS * attempt)
        staged.chmod(staged.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(staged, target)
    finally:
        archive.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
    logger.info("daprd cached at %s", target)


def _daprd_runs(binary: Path) -> bool:
    """True when *binary* executes and reports the pinned version."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, our own cached binary
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("daprd --version failed for %s", binary, exc_info=True)
        return False
    return result.returncode == 0 and _DAPRD_VERSION in result.stdout


def _ensure_daprd_binary() -> Path:
    """Return path to the daprd binary, downloading if absent or broken.

    New downloads are published atomically, but a cache written before that —
    or damaged since — can still hold a truncated binary at this path. An
    existing file is therefore trusted only if it runs and reports the pinned
    version; otherwise it is replaced.
    """
    binary = _CACHE_DIR / _daprd_binary_name()
    if binary.exists() and not _daprd_runs(binary):
        logger.warning("Cached daprd at %s does not run; re-downloading", binary)
        binary.unlink(missing_ok=True)
    if not binary.exists():
        _download_daprd(binary)
    return binary


def _pick_free_port() -> int:
    """Bind a TCP socket to port 0, read the port, release. Race-free enough."""
    return _pick_free_ports(1)[0]


def _pick_free_ports(count: int) -> list[int]:
    """Return *count* distinct free loopback ports.

    Every socket is held open until all ports are read, so the OS cannot hand
    the same port out twice — picking the http and grpc ports one at a time
    (bind, release, bind again) can return the same port for both. Another
    process can still take a port between release and daprd's bind; that case
    surfaces as ``DaprdExitedError`` (daprd exits on a bind failure) rather
    than as a readiness timeout.
    """
    with contextlib.ExitStack() as stack:
        socks = [
            stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            for _ in range(count)
        ]
        for sock in socks:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in socks]


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
    "pubsub.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: pubsub
spec:
  type: pubsub.in-memory
  version: v1
  metadata: []
""",
}


# File-backed secret store, used when a ``secrets_file`` path is supplied (e.g.
# integration tests that assert specific secret values from a checked-in JSON
# file). Hyphenated keys like ``api-key`` round-trip cleanly through a file but
# not through ``local.env``. The component only references the file path — no
# secret values pass through this process.
_SECRET_STORE_FILE_YAML = """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: {name}
spec:
  type: secretstores.local.file
  version: v1
  metadata:
    - name: secretsFile
      value: {secrets_file}
    - name: nestedSeparator
      value: ":"
"""

_FILE_SECRET_STORE_NAMES = ("secretstore", "deployment-secret-store")
# Component filenames re-emitted as file-backed when a secrets file is supplied;
# derived from the names above so adding a store only requires editing one place.
_FILE_SECRET_STORE_FILENAMES = {f"{n}.yaml" for n in _FILE_SECRET_STORE_NAMES}


def _write_components(
    components_dir: Path,
    objectstore_root: Path = Path(DEFAULT_OBJECTSTORE_ROOT),
    eventstore_root: Path = Path(DEFAULT_EVENTSTORE_ROOT),
    secrets_file: str | None = None,
) -> None:
    """Write the auto-generated Dapr component YAMLs into *components_dir*.

    When *secrets_file* (a path to a JSON secrets file) is provided, the
    ``secretstore`` / ``deployment-secret-store`` components are emitted as
    ``secretstores.local.file`` pointing at that file, instead of the default
    env-var store. Only the path is referenced — secret *values* never flow
    through this process.
    """
    components_dir.mkdir(parents=True, exist_ok=True)
    objectstore_root.mkdir(parents=True, exist_ok=True)
    eventstore_root.mkdir(parents=True, exist_ok=True)
    for filename, template in _COMPONENTS_YAML.items():
        # When a secrets file is supplied, skip the default env-var secret
        # store components — they are re-emitted as file-backed below.
        if secrets_file is not None and filename in _FILE_SECRET_STORE_FILENAMES:
            continue
        (components_dir / filename).write_text(
            template.format(
                objectstore_root=str(objectstore_root.resolve()),
                eventstore_root=str(eventstore_root.resolve()),
            ),
            encoding="utf-8",
        )
    if secrets_file is not None:
        resolved = str(Path(secrets_file).resolve())
        for name in _FILE_SECRET_STORE_NAMES:
            (components_dir / f"{name}.yaml").write_text(
                _SECRET_STORE_FILE_YAML.format(name=name, secrets_file=resolved),
                encoding="utf-8",
            )


_LOG_TAIL_BYTES = 4096
_LOG_TAIL_LINES = 20


def _read_log_tail(log_path: Path) -> str:
    """Last few lines of the daprd log, for the error raised when it dies."""
    try:
        with log_path.open("rb") as fh:
            fh.seek(max(0, log_path.stat().st_size - _LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        logger.debug("Could not read daprd log %s", log_path, exc_info=True)
        return ""
    return "\n".join(text.splitlines()[-_LOG_TAIL_LINES:])


async def _wait_for_dapr_ready(
    http_port: int,
    timeout_s: float = 30.0,
    *,
    proc: asyncio.subprocess.Process | None = None,
    log_path: Path | None = None,
) -> None:
    """Poll Dapr's metadata endpoint until it responds or *timeout_s* elapses.

    When *proc* is given, a daprd that has already exited fails the wait at
    once with :class:`DaprdExitedError` (carrying the tail of *log_path*)
    instead of being polled for the full *timeout_s*.
    """
    import httpx  # noqa: PLC0415 — defer the heavy import

    deadline = asyncio.get_event_loop().time() + timeout_s
    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            if proc is not None and proc.returncode is not None:
                raise DaprdExitedError(
                    exit_code=proc.returncode,
                    log_path=str(log_path) if log_path is not None else None,
                    log_tail=_read_log_tail(log_path) if log_path is not None else None,
                )
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
    secrets_file: str | None = None,
    components_dir: str | None = None,
    log_level: str = "warn",
) -> AsyncIterator[EmbeddedDapr]:
    """Boot an embedded ``daprd`` for local app development.

    By default (no *components_dir*) these components are auto-generated into a
    temp dir:

    * ``statestore`` — ``state.in-memory``
    * ``secretstore`` / ``deployment-secret-store`` — ``secretstores.local.env``
      by default, or ``secretstores.local.file`` pointing at *secrets_file*
      (a path to a JSON secrets file) when that is supplied — handy for tests
      that assert specific secret values. Only the path is referenced; secret
      values never flow through this process.
    * ``objectstore`` — ``bindings.localstorage`` rooted at *objectstore_root*
    * ``eventstore`` — ``bindings.localstorage`` rooted at *eventstore_root*
    * ``pubsub`` — ``pubsub.in-memory``

    Pass *components_dir* to supply your own component YAMLs instead of the
    auto-generated set — e.g. an integration harness that needs a
    ``secretstores.local.file`` store in ``multiValued`` mode, or any component
    the defaults don't cover. When given, the SDK still manages the full daprd
    lifecycle (binary download, free-port allocation, boot, readiness wait, env
    save/restore, teardown); it just points ``--resources-path`` at your
    directory and leaves it in place on exit (you own it). *components_dir* and
    *secrets_file* are mutually exclusive, and *objectstore_root* /
    *eventstore_root* are ignored when *components_dir* is set (your components
    define those). The asymmetry is deliberate: *secrets_file* defaults to
    ``None``, so a set value is unambiguous and conflicting with *components_dir*
    is raised; the two roots have non-``None`` defaults, so a deliberately-set
    value can't be told apart from the default and is silently ignored rather
    than erroring.

    Once arguments are validated and the sidecar is about to start, the context
    manager sets ``DAPR_HTTP_PORT``, ``DAPR_GRPC_PORT``, and
    ``DAPR_COMPONENTS_PATH`` so callers (and any background observability flush
    that races with daprd startup) see the right values; the previous values are
    restored on exit. An argument error (mutually-exclusive kwargs, or a
    bad/blank *components_dir*) raises before any env var is touched.
    """
    # Validate arguments up front — before the expensive daprd download and port
    # allocation — so a misconfigured call fails truly fast.
    if components_dir is not None and secrets_file is not None:
        raise DaprComponentsConfigError()
    if components_dir is not None and (
        # Reject blank input explicitly: Path("").is_dir() is True (it resolves
        # to cwd), so a blank components_dir would otherwise slip past and boot
        # daprd against the working directory. An unchecked bad path would
        # otherwise surface ~30s later as a readiness timeout with no hint that
        # the path was the cause.
        not components_dir.strip() or not Path(components_dir).is_dir()
    ):
        raise DaprComponentsDirNotFoundError(value_summary=repr(components_dir))

    binary = _ensure_daprd_binary()
    http_port, grpc_port = _pick_free_ports(2)
    if components_dir is not None:
        # Caller supplies (and owns) the component YAMLs — use them as-is and
        # do not delete the directory on exit (validated above).
        components_path = Path(components_dir)
        _owns_components_dir = False
    else:
        components_path = Path(tempfile.mkdtemp(prefix="atlan-dapr-"))
        _write_components(
            components_path,
            Path(objectstore_root),
            Path(eventstore_root),
            secrets_file=secrets_file,
        )
        _owns_components_dir = True

    # Set env BEFORE spawning the subprocess so the observability sink's
    # flush cycle (which may fire during the ~3s daprd startup window) finds
    # the auto-generated component YAMLs instead of the default ``./components``.
    _prev_env = {
        k: os.environ.get(k)
        for k in ("DAPR_HTTP_PORT", "DAPR_GRPC_PORT", "DAPR_COMPONENTS_PATH")
    }
    os.environ["DAPR_HTTP_PORT"] = str(http_port)
    os.environ["DAPR_GRPC_PORT"] = str(grpc_port)
    os.environ["DAPR_COMPONENTS_PATH"] = str(components_path)

    logger.info("Starting embedded Dapr (http=%d grpc=%d)", http_port, grpc_port)
    # daprd's own output goes to a temp log rather than DEVNULL, so a daprd that
    # dies on startup reports why. The log is kept only in that case.
    log_fd, log_name = tempfile.mkstemp(prefix="atlan-daprd-", suffix=".log")
    log_path = Path(log_name)
    keep_log = False
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--app-id",
            app_id,
            "--dapr-http-port",
            str(http_port),
            "--dapr-grpc-port",
            str(grpc_port),
            "--resources-path",
            str(components_path),
            "--log-level",
            log_level,
            # Disable the metrics + placement endpoints we don't need for local dev.
            "--enable-metrics=false",
            stdout=log_fd,
            stderr=asyncio.subprocess.STDOUT,
        )
    except BaseException:
        # Nothing was spawned, so the cleanup below is never reached.
        log_path.unlink(missing_ok=True)
        raise
    finally:
        # The child holds its own copy of the descriptor.
        os.close(log_fd)
    try:
        try:
            await _wait_for_dapr_ready(http_port, proc=proc, log_path=log_path)
        except DaprdExitedError:
            keep_log = True
            raise
        logger.info("Embedded Dapr ready at http://127.0.0.1:%d", http_port)
        yield EmbeddedDapr(
            http_port=http_port,
            grpc_port=grpc_port,
            components_dir=str(components_path),
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
        # Only clean up the temp dir we generated; a caller-supplied
        # components_dir belongs to the caller.
        if _owns_components_dir:
            # conformance: ignore[P023] local-dev teardown of a self-generated temp dir holding a few component YAMLs — not a data-scaled tree, and an await here would be skipped on the cancellation path this finally exists to serve
            shutil.rmtree(components_path, ignore_errors=True)
        if not keep_log:
            log_path.unlink(missing_ok=True)
        for _k, _v in _prev_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
