"""Readers of installed distribution metadata (the SDK's pins and extras, the scanned repo's environment).

Every other module calls these through the module (``_metadata.<name>(...)``), so
a test patches one attribute here and every caller sees it."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from importlib import metadata as importlib_metadata
from pathlib import Path

from ._common import (
    _SQLALCHEMY_DIALECTS_GROUP,
    SDK_PACKAGE,
    _normalise_name,
    _parse_requirement,
)


def _sdk_managed_packages() -> set[str] | None:
    """Return the set of normalised package names the SDK pins as core deps.

    Reads ``importlib.metadata.requires('atlan-application-sdk')`` and filters
    out optional-extra entries (those with ``extra == ...`` markers). Returns
    ``None`` if the SDK is not importable in the current environment, in which
    case D002 is skipped silently.
    """
    try:
        reqs = importlib_metadata.requires(SDK_PACKAGE)
    except importlib_metadata.PackageNotFoundError:
        return None
    if reqs is None:
        return set()

    out: set[str] = set()
    for req in reqs:
        # ``Requires-Dist: foo>=1; extra == "sql"`` — skip optional-extra deps;
        # an app pulls those in via ``atlan-application-sdk[sql]`` and they are
        # not part of the *core* contract enforced here.
        if "extra ==" in req or 'extra=="' in req or 'extra =="' in req:
            continue
        parsed = _parse_requirement(req)
        if parsed is None:
            continue
        name, _spec = parsed
        out.add(name)
    # Defensive: never claim to manage the SDK itself.
    out.discard(_normalise_name(SDK_PACKAGE))
    return out


def _sdk_published_extras() -> set[str] | None:
    """Return the set of normalised extra names the SDK publishes (D005).

    Reads the ``Provides-Extra`` metadata of ``atlan-application-sdk``.  Names
    are PEP 503-normalised so an app's ``[iam_auth]`` matches the published
    ``iam-auth``.  Returns ``None`` if the SDK is not importable, in which case
    D005 is skipped silently (same posture as D002/D004).
    """
    try:
        meta = importlib_metadata.metadata(SDK_PACKAGE)
    except importlib_metadata.PackageNotFoundError:
        return None
    return {_normalise_name(e) for e in (meta.get_all("Provides-Extra") or [])}


def _repo_site_packages(root: Path) -> list[str]:
    """``site-packages`` directories of the *target repo's* own virtualenv.

    D003 asks "is this dependency imported anywhere?", which needs the import
    names each distribution provides. Resolving those from the *running*
    interpreter makes the answer a property of whoever invoked the suite rather
    than of the repo under test: the same repo yields different D003 findings
    from the SDK's venv, a uvx-provisioned env, and the app's own venv, and the
    environment that resolves the fewest dependencies produces the fewest
    findings — i.e. looks the cleanest while analysing the least.

    So prefer the target repo's environment. Empty list when it has none, in
    which case resolution falls back to the running interpreter as before.
    """
    venv = root / ".venv"
    if not venv.is_dir():
        return []
    candidates = [
        *sorted(venv.glob("lib/python*/site-packages")),
        venv / "Lib" / "site-packages",  # Windows layout
    ]
    return [str(p) for p in candidates if p.is_dir()]


def _env_import_names(search_path: list[str]) -> dict[str, set[str]]:
    """Map each installed target distribution to its provided import names."""
    return _env_distribution_metadata(
        search_path, include_imports=True, include_dialects=False
    )[0]


def _env_distribution_metadata(
    search_path: list[str],
    *,
    include_imports: bool,
    include_dialects: bool,
    dialect_names_for: Collection[str] | None = None,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Read requested metadata maps in one target distribution inventory.

    Empty entry-point sets are retained so a target distribution with no
    registration cannot fall back to a different version in the invoking
    interpreter. *dialect_names_for* (normalised names) limits the entry-point
    read to those distributions — D003 only asks about declared dependencies.
    """
    if not search_path:
        return {}, {}
    imports: dict[str, set[str]] = {}
    dialects: dict[str, set[str]] = {}
    for dist in importlib_metadata.distributions(path=search_path):
        name = dist.metadata["Name"] if dist.metadata else None
        if not name:
            continue
        normalised = _normalise_name(name)
        if include_imports:
            imports[normalised] = _provided_import_names(dist)
        if include_dialects and (
            dialect_names_for is None or normalised in dialect_names_for
        ):
            dialects[normalised] = _dialect_entry_point_names(dist)
    return imports, dialects


def _dist_import_names(
    dist_name: str, *, env_map: Mapping[str, set[str]] | None = None
) -> set[str] | None:
    """Return the top-level import names a distribution provides, or ``None``.

    Resolution order: the target repo's own environment (*env_map*, built by
    :func:`_env_import_names`), then the running interpreter. The fallback only
    ever widens coverage — a repo env installed with ``--no-dev`` or a partial
    sync can still have a dependency resolvable in the invoking interpreter.

    Returns ``None`` when neither can resolve it — the caller then *skips* the
    dependency (never flags it) and reports it as unanalysed, so a missing env
    can never produce a false "unused" finding.
    """
    if env_map is not None:
        provided = env_map.get(_normalise_name(dist_name))
        if provided is not None:
            return provided
    try:
        dist = importlib_metadata.distribution(dist_name)
    except importlib_metadata.PackageNotFoundError:
        return None
    return _provided_import_names(dist)


def _env_dialect_entry_points(search_path: list[str]) -> dict[str, set[str]]:
    """Map every target distribution to its dialect entry points, including none."""
    return _env_distribution_metadata(
        search_path, include_imports=False, include_dialects=True
    )[1]


def _dist_dialect_entry_points(
    dist_name: str, *, env_map: Mapping[str, set[str]] | None = None
) -> set[str]:
    """Return the ``sqlalchemy.dialects`` entry points *dist_name* registers.

    Same resolution order as :func:`_dist_import_names`: the target repo's
    environment first, then the running interpreter. Empty when neither has it
    — the dependency then falls through to the ordinary unused check.
    """
    if env_map is not None:
        normalised = _normalise_name(dist_name)
        if normalised in env_map:
            return env_map[normalised]
    try:
        dist = importlib_metadata.distribution(dist_name)
    except importlib_metadata.PackageNotFoundError:
        return set()
    return _dialect_entry_point_names(dist)


def _dialect_entry_point_names(dist: importlib_metadata.Distribution) -> set[str]:
    return {
        ep.name for ep in dist.entry_points if ep.group == _SQLALCHEMY_DIALECTS_GROUP
    }


def _provided_import_names(dist: importlib_metadata.Distribution) -> set[str]:
    """Top-level import names *dist* provides, from ``top_level.txt`` when
    present plus top-level entries derived from its file list (RECORD)."""
    names: set[str] = set()
    # Distribution.read_text takes a filename positionally and returns None when
    # absent — a metadata lookup with no decode step, so the read-guard
    # invariant (tests/test_read_guard_invariant.py) exempts it explicitly.
    top_level = dist.read_text("top_level.txt")  # read-guard: exempt
    if top_level:
        names.update(line.strip() for line in top_level.splitlines() if line.strip())

    # Derive top-levels from the installed file list as a fallback / supplement
    # (namespace packages and wheels without top_level.txt rely on this).  Each
    # candidate is gated on ``isidentifier`` so non-module RECORD entries — data
    # files (``share/…``), scripts installed via ``..`` / ``bin``, ``LICENSE`` —
    # never leak into the provided-names set.
    for file in dist.files or ():
        parts = file.parts
        if not parts:
            continue
        head = parts[0]
        if head.endswith((".dist-info", ".data", ".egg-info")):
            continue
        if len(parts) == 1:
            # A single top-level module file: ``foo.py`` -> ``foo``.
            if head.endswith(".py"):
                stem = head[:-3]
                if stem.isidentifier():
                    names.add(stem)
        elif head.isidentifier():
            # A package directory: ``foo/__init__.py`` -> ``foo``.
            names.add(head)

    return names
