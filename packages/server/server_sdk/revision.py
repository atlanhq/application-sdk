"""``server_revision`` — the three-part build identity every response carries.

Consolidation collapses five app servers into one process, so "which build is
answering?" stops being answerable from the pod name. This module computes the
identity agreed in the ARUN-942 thread. It is deliberately a **triple**, not one
number, because the three parts answer three different questions and have three
different trust levels:

``app_source_digest``
    A content hash of *this app's* server package source. Two apps in one host
    have two different values — that is the whole point. It is computable
    identically from (a) the installed distribution, via the ``dist-info``
    ``RECORD``, and (b) the app repo's source tree (see
    :func:`source_digest_from_tree`), so CI can assert that the image running in
    production was built from a given commit without trusting a build label.

``server_sdk_declared_rev``
    What the app *declares* it wants from ``atlan-application-sdk-server`` (or the
    legacy ``atlan-server-sdk``), read straight out
    of the installed distribution's ``METADATA`` ``Requires-Dist``. For the
    hosted apps this is a PEP 508 direct reference carrying a pinned git rev, so
    it is the honest answer to "which server-sdk commit was this app pinned to
    when it was built". It is a *declaration*, not an observation: it says what
    the app asked for, not what the resolver actually installed.

``env_digest``
    A digest of the resolved dependency closure of the **running process**.

    HOST-SIDE ONLY, and a forensic breadcrumb — never an alert source.

    The app repos cannot compute this and cannot be expected to match it. Two
    verified reasons: (1) neither ``atlan-redshift-app`` nor
    ``atlan-snowflake-app`` covers its own ``server/`` package in its root
    ``uv.lock`` (``grep -c server-sdk`` on both returns zero), so the repo lock
    simply does not describe the serving closure; and (2) each repo resolves the
    serving dependencies differently anyway — fastapi came out 0.141.1 in
    common-app-server, 0.139.2 in redshift and 0.137.2 in snowflake. A
    consolidated host therefore has *one* env_digest that will differ from every
    app repo's idea of one, by construction. Alerting on a mismatch would page
    on the normal case. Use it only to answer "were these two pods running the
    same closure?" after the fact.

Design constraints this module holds to:

* **No process-global env is read for identity.** ``ATLAN_APPLICATION_NAME`` in
  a consolidated host names the *host* (``common-app-server``), not the hosted
  app, so reading it here would stamp every app with the host's identity — the
  same class of defect already found three times in the manifest task-queue
  path. The package to digest is passed in by the app.
* **No new runtime dependency.** ``hashlib``, ``importlib.metadata``, ``csv``,
  ``base64`` — stdlib only. server-sdk core stays fastapi/pydantic/starlette/
  orjson.
* **Never raises.** Every part degrades to ``None`` (rendered ``unknown``). A
  build-identity stamp that can 500 a request is worse than no stamp.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import importlib.metadata as importlib_metadata
import importlib.util
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# The distribution whose Requires-Dist entry carries the declared rev.
SERVER_SDK_DIST = "atlan-application-sdk-server"

#: The pre-consolidation name (ARUN-942). The fleet migrates app-by-app, and a
#: host process serves several apps at once, so an app still declaring the
#: standalone distribution has to keep a working stamp — matching only the
#: current name would silently return ``None`` for it, and a null declared rev
#: is what the host re-pin trigger reads. Drop once no app declares it.
SERVER_SDK_DIST_LEGACY = "atlan-server-sdk"

#: Accepted names in preference order: an app declaring both stamps the current.
SERVER_SDK_DISTS: tuple[str, ...] = (SERVER_SDK_DIST, SERVER_SDK_DIST_LEGACY)

#: The Python half of ``app_source_digest``. Every backend ships the declared
#: package's modules verbatim, so these need no build config to enumerate.
SOURCE_SUFFIXES: frozenset[str] = frozenset({".py", ".pyi"})

#: The data half. These ship inside the package and the *serving path reads them
#: at request time* — a generated contract, a workflow manifest — so a change to
#: one changes what the app serves just as surely as a change to a module.
#:
#: They were deliberately excluded once, to keep the digest independent of build
#: config: whether a data file reaches the wheel depends on ``force-include`` /
#: ``artifacts`` / ``package-data``, which differ per app. That reasoning was
#: half right and the wrong half was load-bearing. The dist side reads ``RECORD``,
#: which lists what the wheel *actually* carries, data files included; only the
#: source side ever needed the build config, and it already reads it — that is
#: what :func:`overlays_from_force_include` is. Excluding data bought symmetry
#: the two sides did not need and cost the digest its purpose: gov's
#: ``app/generated/**/manifest.json`` is force-included, served from
#: ``/manifest``, and edits to it moved no digest, so ``bump-app-pin`` reported
#: "unchanged" and the host served a stale manifest indefinitely (ARUN-942).
#:
#: The residual constraint, and the reason this is an allowlist rather than
#: "every file": the source side walks the checkout, so a data file sitting in
#: the package directory *without* reaching the wheel would move the source
#: digest and never the image's — a permanent false drift. Keep data out of the
#: package tree in the repo and let it arrive by graft, as all three hosted apps
#: do today (their checked-out package dirs hold ``.py`` only). Widening this set
#: re-pins every hosted app once, by design: the digest is meant to move when the
#: rule for "what the app serves" moves.
DATA_SUFFIXES: frozenset[str] = frozenset({".json", ".yaml", ".yml"})

#: Everything that takes part in ``app_source_digest``, on both sides.
DIGESTED_SUFFIXES: frozenset[str] = SOURCE_SUFFIXES | DATA_SUFFIXES

#: Directory names pruned from both sides. ``__pycache__`` is install-time
#: output: it exists in a container layer but never in ``RECORD`` with a usable
#: hash, and its contents depend on the interpreter, not on the source.
PRUNED_DIRS: frozenset[str] = frozenset({"__pycache__"})

#: Truncation for every digest this module emits. 64 bits is ample for
#: "did this change?" and short enough to read off a header in a terminal.
DIGEST_LEN = 16

#: Rendered in place of a part that could not be determined.
UNKNOWN = "unknown"

# Header values must survive an HTTP hop unescaped. Requires-Dist entries carry
# spaces, semicolons and quotes (``... ; extra == 'workflow'``), and ``;``/``=``
# are our own delimiters — so anything outside this set is folded to ``_``.
_HEADER_UNSAFE = re.compile(r"[^A-Za-z0-9._~:/@+-]")
_HEADER_PART_MAX = 160


# ---------------------------------------------------------------------------
# The value
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServerRevision:
    """The three-part build identity. Any part may be ``None``."""

    app_source_digest: str | None = None
    server_sdk_declared_rev: str | None = None
    env_digest: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        """JSON-body shape. Keeps ``None`` — a body can express "unknown" exactly."""
        return {
            "app_source_digest": self.app_source_digest,
            "server_sdk_declared_rev": self.server_sdk_declared_rev,
            "env_digest": self.env_digest,
        }

    def as_header(self) -> str:
        """Compact ``src=..;sdk=..;env=..`` form for ``X-Atlan-Server-Revision``.

        Lossy on purpose: the full, unsanitized ``server_sdk_declared_rev`` (a
        PEP 508 requirement string, spaces and all) lives in the manifest JSON
        body. The header is the grep-able form.
        """
        return ";".join(
            f"{key}={header_safe(value)}"
            for key, value in (
                ("src", self.app_source_digest),
                ("sdk", self.server_sdk_declared_rev),
                ("env", self.env_digest),
            )
        )


def header_safe(value: str | None) -> str:
    """Fold ``value`` into something that can be an HTTP header value verbatim.

    Public because ``as_header`` is not the only caller that needs it.
    ``app_version`` comes from a distribution's ``Version`` field and reaches
    the wire on its own header, so it has to pass through the *same* filter.
    Sanitizing one half of the stamp and not the other is how a ``CRLF`` in a
    version string turns into a second, attacker-chosen header:
    ``str.encode("ascii", "replace")`` stops neither CR nor LF, because both are
    already ASCII.

    Empty / ``None`` renders :data:`UNKNOWN`, so the header is always present
    and always parseable.
    """
    if not value:
        return UNKNOWN
    return _HEADER_UNSAFE.sub("_", value)[:_HEADER_PART_MAX]


# ---------------------------------------------------------------------------
# Digest primitives
# ---------------------------------------------------------------------------


def _fold(pairs: Iterable[tuple[str, str]]) -> str:
    """Fold ``(relative posix path, sha256 hex)`` pairs into one short digest.

    Sorted, length-delimited and content-only: the digest is a function of the
    file *names* and file *bytes*, so it is invariant under mtime, permission,
    owner, and directory-iteration order changes — the properties that make it
    comparable between a git checkout and a container layer.
    """
    hasher = hashlib.sha256()
    for rel, file_hex in sorted(pairs):
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(file_hex.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()[:DIGEST_LEN]


def _sha256_file(path: Path) -> str:
    """sha256 of the file's **raw bytes** — no newline normalization. Ever.

    That is a constraint on the contract, not an oversight, and it is
    load-bearing in both directions:

    * The image side reads its hashes out of ``RECORD``, which PEP 376 defines
      as the sha256 of the installed bytes. Normalizing newlines here and not
      there would make the two sides disagree by construction — and the RECORD
      side *cannot* normalize, because it never sees the bytes.
    * Normalizing would blunt the digest. A file whose only change is its line
      endings is a different file in the image, and the point of
      ``app_source_digest`` is to notice that the image is not what the repo
      says it is.

    The cost is that the digest is **line-ending sensitive**, so the comparison
    carries a precondition: *both sides must materialize identical bytes*. A
    Windows checkout with ``core.autocrlf=true`` hashes CRLF where the Linux
    build hashed LF, and its repo-side digest can never equal the image's — a
    false "drift" that looks exactly like a real one. Compute the repo side on
    Linux (CI does), and/or commit ``.gitattributes`` with ``* text=auto
    eol=lf`` so every checkout holds the bytes the image was built from.
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_digested(rel: str) -> bool:
    """Does this package-relative path take part in ``app_source_digest``?"""
    parts = rel.split("/")
    if any(part in PRUNED_DIRS for part in parts[:-1]):
        return False
    return PurePosixPath(rel).suffix in DIGESTED_SUFFIXES


# ---------------------------------------------------------------------------
# app_source_digest — source-tree side
# ---------------------------------------------------------------------------


def _walk_tree(root: Path, prefix: str = "") -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        rel = f"{prefix}/{rel}" if prefix else rel
        if not _is_digested(rel):
            continue
        pairs.append((rel, _sha256_file(path)))
    return pairs


def source_digest_from_tree(
    package_root: Path | str,
    *,
    overlays: Mapping[str, Path | str] | None = None,
) -> str | None:
    """Digest an app's server package straight from a checkout.

    ``package_root`` is the package directory itself (the one holding
    ``__init__.py``) — e.g. ``atlan-redshift-app/server/redshift_server``. Paths
    are recorded relative to it, so the digest does not depend on where in the
    repo the package sits or on the distribution's name.

    ``overlays`` mirrors a build backend's file grafting so the checkout can
    reproduce the *installed* layout. Each key is the path the grafted content
    occupies **inside the package**; each value is where that content lives in
    the checkout, and may be a directory or a single file.

    Do not hand-write this mapping. It is not the same shape for every app, and
    getting it wrong is silent: it yields a repo-side digest that can never
    equal the image's, which reads exactly like a real drift alert. Derive it
    from the app's own ``force-include`` with
    :func:`overlays_from_force_include`, which encodes the one rule that
    matters: **overlay exactly what the graft names, and nothing enclosing it.**

    The two shapes in the fleet today are why that rule cannot be shortcut:

    * ``atlan-redshift-app/server/pyproject.toml`` (likewise snowflake and
      governance) grafts a whole directory::

          force-include = { "../app/generated" = "redshift_server/generated" }

      The wheel therefore carries ``redshift_server/generated/**`` — both the
      ``.py`` files (the package ``__init__.py``, ``crawler/_input.py``) and the
      generated contract JSON — that the checked-out ``server/redshift_server``
      does not. Those apps need ``{"generated": repo / "app/generated"}``.
    * ``atlan-memory-app`` grafts two individual ``.json`` files rather than a
      directory, so its overlay set names those two files. Passing the enclosing
      directory instead — the obvious generalization — folds in siblings that
      never reach that app's wheel and guarantees a mismatch.

    Apps with no ``force-include`` pass nothing.

    Returns ``None`` when the tree holds no source files at all — an explicit
    "could not determine", never a digest of emptiness that would silently
    compare equal across two different apps.
    """
    root = Path(package_root)
    if not root.is_dir():
        return None
    pairs = _walk_tree(root)
    for mount, source in (overlays or {}).items():
        overlay_path = Path(source)
        mount_rel = mount.strip("/")
        if overlay_path.is_dir():
            pairs.extend(_walk_tree(overlay_path, prefix=mount_rel))
        elif overlay_path.is_file():
            # A single-file graft (atlan-memory-app's shape) — a .json manifest
            # counts here exactly as a module would.
            if _is_digested(mount_rel):
                pairs.append((mount_rel, _sha256_file(overlay_path)))
        else:
            logger.warning(
                "revision: overlay %r -> %s does not exist; skipped",
                mount,
                overlay_path,
            )
    if not pairs:
        return None
    return _fold(pairs)


def overlays_from_force_include(
    force_include: Mapping[str, str],
    *,
    base: Path | str,
    package: str,
) -> dict[str, Path]:
    """Derive :func:`source_digest_from_tree` ``overlays`` from ``force-include``.

    ``force_include`` is hatchling's mapping verbatim — ``{source relative to
    the pyproject: destination inside the wheel}``. ``base`` is the directory
    that pyproject lives in; sources resolve against it. ``package`` is the
    import name being digested — a graft landing outside it cannot move that
    package's digest and is dropped.

    Entries contributing nothing in :data:`DIGESTED_SUFFIXES` are dropped too —
    a grafted ``.so`` or README moves no digest and overlaying it would only add
    a way to disagree with the wheel::

        # redshift / snowflake / governance: a directory graft
        >>> overlays_from_force_include(
        ...     {"../app/generated": "redshift_server/generated"},
        ...     base=repo / "server", package="redshift_server",
        ... )
        {'generated': PosixPath('.../app/generated')}

        # atlan-memory-app: two grafted .json files, each overlaid by name
        >>> overlays_from_force_include(
        ...     {"../app/generated/ai-memory.json":
        ...          "ai_memory_server/generated/ai-memory.json",
        ...      "../app/generated/manifest.json":
        ...          "ai_memory_server/generated/manifest.json"},
        ...     base=repo / "server", package="ai_memory_server",
        ... )
        {'generated/ai-memory.json': PosixPath('.../app/generated/ai-memory.json'),
         'generated/manifest.json': PosixPath('.../app/generated/manifest.json')}

    ``package`` is the IMPORT name — exactly the string in the app's
    ``[tool.hatch.build.targets.wheel] packages`` — not the distribution name
    and not a shortened form of it. Every graft is matched against that prefix,
    so a wrong ``package`` silently yields ``{}`` here and, passed on to
    :func:`server_revision`, an ``src=unknown`` stamp forever rather than an
    error. atlan-memory-app ships ``ai_memory_server`` (dist
    ``atlan-ai-memory-server``); ``memory_server`` matches nothing.

    Never raises: an unreadable or missing source is simply not overlaid.
    """
    base_dir = Path(base)
    package_prefix = package.replace(".", "/") + "/"
    derived: dict[str, Path] = {}
    for source, destination in force_include.items():
        target = str(destination).replace("\\", "/").strip("/")
        if not target.startswith(package_prefix):
            continue
        mount = target[len(package_prefix) :]
        if not mount:
            continue
        candidate = base_dir / source
        try:
            if candidate.is_file():
                if _is_digested(mount):
                    derived[mount] = candidate
                continue
            if not candidate.is_dir():
                continue
            if any(
                _is_digested(f"{mount}/{child.relative_to(candidate).as_posix()}")
                for child in candidate.rglob("*")
                if child.is_file()
            ):
                derived[mount] = candidate
        except OSError:  # pragma: no cover - unreadable path
            continue
    return derived


# ---------------------------------------------------------------------------
# app_source_digest — installed-distribution side
# ---------------------------------------------------------------------------


def _record_hash_to_hex(spec: str) -> str | None:
    """``sha256=<urlsafe-b64, unpadded>`` (PEP 376 RECORD) -> hex.

    Anything else — a different algorithm, or the empty hash RECORD uses for
    files it cannot hash — returns ``None``, which aborts the RECORD path in
    favour of hashing the installed files directly. Comparing a non-sha256
    RECORD hash against our own sha256 would silently produce a digest that
    never matches the source side.
    """
    algo, _, encoded = spec.partition("=")
    if algo != "sha256" or not encoded:
        return None
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(padded).hex()
    except (binascii.Error, ValueError):
        return None


def source_digest_from_record(
    dist: importlib_metadata.Distribution, package: str
) -> str | None:
    """Digest an app's server package from its installed ``dist-info/RECORD``.

    RECORD already carries a sha256 per installed file, so this reads one small
    text file instead of walking and hashing the package — the cheap path, and
    the one that works in a distroless image where the source tree is gone.

    **A RECORD is trusted only when it is authoritative for the package**, and
    that is decided by one check: does it list the package's own top-level
    ``<package>/__init__.py``? A non-empty set of matching rows is *not* enough.
    For every force-include app (redshift, snowflake, governance) an
    **editable** RECORD is not empty for the package — it lists the grafted
    ``<package>/generated/**`` subtree, ``.py`` files and all, and none of the
    actual server modules, which are reached through a ``.pth`` shim rather than
    installed::

        _editable_impl_atlan_redshift_server.pth,sha256=...
        redshift_server/generated/__init__.py,sha256=...
        redshift_server/generated/crawler/_input.py,sha256=...
        # ...and no redshift_server/__init__.py, no redshift_server/handler.py

    Trusting that produces a digest of the overlay *alone*: it describes almost
    none of the app, it can never equal the wheel's digest, and two apps whose
    generated trees coincide would collide on it. Hence the check — and hence
    the documented fallback actually firing where it always claimed to.

    Returns ``None`` for such a RECORD, and for one listing no source file for
    ``package`` at all. Callers fall back to :func:`source_digest_from_tree` on
    the resolved import location, which for an editable install *is* the app
    repo checkout (the ``.pth`` shim points at it, and a regular package there
    wins over the bare ``<package>/`` namespace portion left in site-packages).
    That is an honest digest of the code actually being served in dev. It is
    deliberately not claimed to equal the *image's* digest for a force-include
    app: the wheel also carries the grafted subtree, which the bare checkout
    does not. Reproducing the image from a checkout is
    :func:`source_digest_from_tree` with ``overlays`` — see
    :func:`overlays_from_force_include`.

    A wheel install always lists ``<package>/__init__.py``. A PEP 420 namespace
    package legitimately has none; it takes the fallback, which walks the same
    installed directory and reaches the same digest, just less cheaply.
    """
    try:
        record = dist.read_text("RECORD")
    except OSError:  # pragma: no cover - unreadable dist-info
        return None
    if not record:
        return None

    prefix = package.replace(".", "/") + "/"
    rows = [
        (row[0].replace("\\", "/"), row[1])
        for row in csv.reader(io.StringIO(record))
        if len(row) >= 2
    ]

    if not any(installed == f"{prefix}__init__.py" for installed, _ in rows):
        logger.debug(
            "revision: RECORD for %s lists no %s__init__.py, so it is not "
            "authoritative for the package (an editable install lists only the "
            "force-included subtree); falling back to the resolved source tree",
            package,
            prefix,
        )
        return None

    pairs: list[tuple[str, str]] = []
    for installed, hash_cell in rows:
        if not installed.startswith(prefix):
            continue
        rel = installed[len(prefix) :]
        if not _is_digested(rel):
            continue
        file_hex = _record_hash_to_hex(hash_cell)
        if file_hex is None:
            logger.debug(
                "revision: RECORD entry %r has no usable sha256; "
                "abandoning the RECORD path for %s",
                installed,
                package,
            )
            return None
        pairs.append((rel, file_hex))

    if not pairs:
        return None
    return _fold(pairs)


def _resolved_package_dir(package: str) -> Path | None:
    """Where ``package`` actually lives on disk, without importing it."""
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError, AttributeError):
        return None
    if spec is None:
        return None
    locations = list(spec.submodule_search_locations or [])
    if spec.origin and spec.origin != "namespace":
        return Path(spec.origin).parent
    for location in locations:
        candidate = Path(location)
        if candidate.is_dir():
            return candidate
    return None


def app_source_digest(
    package: str, dist: importlib_metadata.Distribution | None = None
) -> str | None:
    """RECORD first, resolved-on-disk tree second. ``None`` if neither works."""
    if dist is not None:
        digest = source_digest_from_record(dist, package)
        if digest is not None:
            return digest
    package_dir = _resolved_package_dir(package)
    if package_dir is None:
        return None
    return source_digest_from_tree(package_dir)


# ---------------------------------------------------------------------------
# server_sdk_declared_rev
# ---------------------------------------------------------------------------

_REQ_NAME_END = re.compile(r"[\s\[\](<>=!~;@]")


def _canonical(name: str) -> str:
    """PEP 503 normalization, so ``Atlan_Server.SDK`` == ``atlan-server-sdk``."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _requirement_name(requirement: str) -> str:
    stripped = requirement.strip()
    match = _REQ_NAME_END.search(stripped)
    return stripped[: match.start()] if match else stripped


def declared_server_sdk_rev(
    dist: importlib_metadata.Distribution | None,
    *,
    sdk_dist: str | Sequence[str] = SERVER_SDK_DISTS,
) -> str | None:
    """The app's ``Requires-Dist`` entry for the server distribution, verbatim.

    ``sdk_dist`` defaults to :data:`SERVER_SDK_DISTS`, so the current
    ``atlan-application-sdk-server`` is preferred and the legacy
    ``atlan-server-sdk`` is still accepted.

    Returned unparsed and unnormalized so the pinned git rev in a PEP 508 direct
    reference survives intact::

        atlan-application-sdk-server[aws,sql] @ git+https://github.com/atlanhq/application-sdk.git@94ece49...

    An app pinning the SDK through a marker-gated extra also declares an
    unconditional entry; the unconditional one wins, so what is returned is the
    app's own base-install requirement rather than a marker-gated variant.

    This is what the app DECLARES, not what is installed. The value is read from
    the app distribution's ``METADATA``, and a resolver-level override replaces
    the requirement at resolution time WITHOUT rewriting that metadata — so the
    two diverge silently. That is the live case, not a hypothetical: the
    consolidated common-app-server carries a ``[tool.uv] override-dependencies``
    entry forcing ``atlan-server-sdk`` ahead of the rev the five hosted app
    packages declare, so every response there stamps the declared rev while a
    different rev is the one actually running. Treat the ``sdk=`` part of
    ``X-Atlan-Server-Revision`` as a declaration, and read the installed SDK rev
    from the SDK distribution itself (``pip show`` / its ``direct_url.json``).

    ``None`` when the distribution is absent or names no such requirement — an
    app installed from a tree with no metadata is a degraded stamp, not an error.
    """
    if dist is None:
        return None
    try:
        requirements = dist.metadata.get_all("Requires-Dist") or []
    except Exception:  # pragma: no cover - malformed METADATA
        return None

    targets = (
        (_canonical(sdk_dist),)
        if isinstance(sdk_dist, str)
        else tuple(_canonical(name) for name in sdk_dist)
    )
    # Ordered, so a name earlier in `targets` wins outright over a later one.
    for target in targets:
        fallback: str | None = None
        for raw in requirements:
            requirement = str(raw).strip()
            if _canonical(_requirement_name(requirement)) != target:
                continue
            if ";" not in requirement:  # unconditional — the base install's rev
                return requirement
            if fallback is None:
                fallback = requirement
        if fallback is not None:
            return fallback
    return None


# ---------------------------------------------------------------------------
# env_digest  (HOST-SIDE ONLY — see the module docstring)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def environment_digest() -> str | None:
    """Digest of every distribution installed in the running interpreter.

    HOST-SIDE ONLY. A forensic breadcrumb, never an alert source: see the module
    docstring for why an app repo structurally cannot reproduce this value, and
    why alerting on a mismatch would fire on the normal case.

    Cached for the process lifetime. ``maxsize=1`` is exact rather than
    arbitrary: the function takes no arguments, so one entry *is* the whole
    cache. This is not a micro-optimization — it was the only part of the triple
    left uncached. :func:`server_revision`'s cache is keyed on ``(app_package,
    dist_name)``, so a consolidated host hosting N apps ran the full
    distribution scan N times at ``build_asgi_app`` time — enumerating every
    distribution on ``sys.path`` and forcing a full ``METADATA`` parse for each
    — to produce the identical value every time, because the closure describes
    the *process*, not the app.

    Call ``environment_digest.cache_clear()`` if the closure changes under you
    (tests do; production does not — a running interpreter does not grow new
    distributions).
    """
    try:
        closure = {
            (_canonical(name), dist.version or "")
            for dist in importlib_metadata.distributions()
            if (name := (dist.metadata["Name"] or "").strip())
        }
    except Exception:  # pragma: no cover - a broken sys.path entry
        logger.debug("revision: could not enumerate distributions", exc_info=True)
        return None
    if not closure:
        return None
    return _fold(
        (name, hashlib.sha256(version.encode()).hexdigest())
        for name, version in closure
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _packages_to_distributions() -> Mapping[str, list[str]]:
    try:
        return importlib_metadata.packages_distributions()
    except Exception:  # pragma: no cover - depends on sys.path health
        return {}


def find_distribution(
    package: str | None, dist_name: str | None = None
) -> importlib_metadata.Distribution | None:
    """Resolve the installed distribution that ships ``package``.

    ``dist_name`` wins when given. Otherwise the import-name → distribution-name
    map is tried, then the conventional ``foo_bar`` → ``foo-bar`` guess. All
    misses return ``None``; nothing here raises.
    """
    candidates: list[str] = []
    if dist_name:
        candidates.append(dist_name)
    elif package:
        candidates.extend(_packages_to_distributions().get(package, []))
        candidates.append(package.replace("_", "-"))
    for candidate in candidates:
        try:
            return importlib_metadata.distribution(candidate)
        except importlib_metadata.PackageNotFoundError:
            continue
        except Exception:  # pragma: no cover - malformed dist-info
            continue
    return None


def compute_server_revision(
    app_package: str | None,
    *,
    dist_name: str | None = None,
) -> ServerRevision:
    """Compute the full triple for one app package. Never raises.

    ``app_package`` is the *import* name of the app's server package (e.g.
    ``redshift_server``) — passed in by the app, never sniffed from the
    environment. ``None`` yields a fully-unknown revision rather than an error,
    so an app that has not adopted the stamp still serves.
    """
    dist = find_distribution(app_package, dist_name)
    try:
        digest = app_source_digest(app_package, dist) if app_package else None
    except Exception:  # pragma: no cover - defensive; a stamp must not 500
        logger.warning(
            "revision: app_source_digest failed for %s", app_package, exc_info=True
        )
        digest = None
    return ServerRevision(
        app_source_digest=digest,
        server_sdk_declared_rev=declared_server_sdk_rev(dist),
        env_digest=environment_digest(),
    )


@lru_cache(maxsize=64)
def server_revision(
    app_package: str | None = None, dist_name: str | None = None
) -> ServerRevision:
    """Cached :func:`compute_server_revision` — the accessor request paths use.

    The inputs are immutable for the life of the process (the package's bytes
    are baked into the image), so this is computed once per app and then costs a
    dict lookup. That matters more than usual here: in a consolidated host every
    hosted app shares one event loop with the kubelet probes, and a per-request
    ``rglob`` over a package is exactly the synchronous work that was measured
    pushing ``/server/health`` past its 1s probe timeout.

    Call ``server_revision.cache_clear()`` if the environment changes under you
    (tests do; production does not).
    """
    return compute_server_revision(app_package, dist_name=dist_name)


def resolve_app_version(
    app_package: str | None = None,
    dist_name: str | None = None,
    explicit: str | None = None,
) -> str:
    """The app's version: explicit override, else its distribution's Version."""
    if explicit:
        return explicit
    dist = find_distribution(app_package, dist_name)
    if dist is None:
        return UNKNOWN
    return (dist.version or "").strip() or UNKNOWN


__all__ = [
    "DATA_SUFFIXES",
    "DIGESTED_SUFFIXES",
    "DIGEST_LEN",
    "PRUNED_DIRS",
    "SERVER_SDK_DIST",
    "SERVER_SDK_DISTS",
    "SERVER_SDK_DIST_LEGACY",
    "SOURCE_SUFFIXES",
    "UNKNOWN",
    "ServerRevision",
    "app_source_digest",
    "compute_server_revision",
    "declared_server_sdk_rev",
    "environment_digest",
    "find_distribution",
    "header_safe",
    "overlays_from_force_include",
    "resolve_app_version",
    "server_revision",
    "source_digest_from_record",
    "source_digest_from_tree",
]
