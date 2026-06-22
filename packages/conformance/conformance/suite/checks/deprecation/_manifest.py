"""The deprecated-symbol manifest — committed data that makes B001 fleet-wide.

B001 runs inside *consumer apps*, which do not have the SDK *source* on hand —
only the installed package.  So the set of "what is deprecated" is captured once,
on the SDK side, as a committed JSON artifact that travels with this conformance
package's version.  When the SDK adds a ``@deprecated`` the manifest is
regenerated in the same PR (enforced by ``tests/test_deprecations_manifest.py``),
and every app calling the reusable workflow at ``@main`` picks it up with **zero
per-app work**.

This module is the single definition of the manifest's shape, its on-disk
location, and how it is built from SDK source — shared by the generator
(``conformance.tools.generate_deprecations``) and the B001 consumer detector so
the producer and the reader can never disagree about the format.
"""

from __future__ import annotations

import ast
import importlib.resources as _ir
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import discover

from ._extractor import extract_sites

_MANIFEST_RELPATH = ("data", "deprecated_symbols.json")


def _manifest_path() -> Path:
    """Resolve the committed manifest as a filesystem path for writing.

    Uses ``importlib.resources`` (like ``cli._cmd_programs_dir``) rather than a
    ``__file__``-relative path, so it resolves correctly across editable installs
    and built wheels alike — the manifest ships inside the ``conformance`` package
    (verified: it appears in the wheel under ``conformance/data/``).  Reading goes
    through :func:`load_manifest`, which uses the resource Traversable directly so
    it stays correct even for zip-imported / namespace-packaged layouts.
    """
    return Path(str(_ir.files("conformance"))).joinpath(*_MANIFEST_RELPATH)


# Committed manifest location (package data) — for the generator's write target
# and the drift test; reads use the resource Traversable (see load_manifest).
MANIFEST_PATH = _manifest_path()

# The SDK import root whose deprecations we track.
SDK_IMPORT_ROOT = "application_sdk"


@dataclass(frozen=True)
class DeprecatedSymbol:
    """One marked deprecated symbol, as recorded in the manifest."""

    symbol: str
    kind: str
    module: str
    marker_via: str
    message: str
    migration_target: bool
    removal_version: str | None


@dataclass(frozen=True)
class Manifest:
    """The full deprecated-symbol manifest.

    Intentionally carries no ``sdk_version``: B003 reads the repo's *current*
    ``[project].version`` at scan time, so no detector needs a version baked into
    the manifest — and omitting it keeps the drift test (which byte-compares this
    file) from going red on every routine version bump.
    """

    symbols: tuple[DeprecatedSymbol, ...]

    def symbols_named(self, name: str) -> list[DeprecatedSymbol]:
        """All deprecated records with symbol == *name* (usually 0 or 1)."""
        return [s for s in self.symbols if s.symbol == name]


# ---------------------------------------------------------------------------
# Build (SDK side)
# ---------------------------------------------------------------------------


def _module_path(file: Path, sdk_root: Path) -> str:
    """Derive the dotted module path for *file* under *sdk_root*.

    ``application_sdk/discovery.py`` → ``application_sdk.discovery``;
    ``application_sdk/app/__init__.py`` → ``application_sdk.app``.
    """
    rel = file.relative_to(sdk_root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def build_manifest(sdk_root: Path) -> Manifest:
    """Scan ``<sdk_root>/application_sdk`` and build the manifest of marked symbols.

    Only *marked* symbols (decorator or class-attributable warn) are recorded —
    claim-only sites are an authoring concern (B004), not a consumer signal.
    """
    package_root = sdk_root / SDK_IMPORT_ROOT
    records: list[DeprecatedSymbol] = []
    for file in discover(package_root):
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except (OSError, SyntaxError):
            continue
        module = _module_path(file, sdk_root)
        for site in extract_sites(tree):
            if site.marker_via is None:
                continue
            records.append(
                DeprecatedSymbol(
                    symbol=site.symbol,
                    kind=site.kind,
                    module=module,
                    marker_via=site.marker_via,
                    message=site.message,
                    migration_target=site.has_migration_target,
                    removal_version=site.removal_version_raw,
                )
            )
    # Deterministic order for stable diffs: (module, symbol).
    records.sort(key=lambda r: (r.module, r.symbol))
    return Manifest(symbols=tuple(records))


# ---------------------------------------------------------------------------
# Serialise / load
# ---------------------------------------------------------------------------


def serialize(manifest: Manifest) -> str:
    """Render *manifest* to canonical JSON (sorted keys, trailing newline)."""
    payload = {"symbols": [asdict(s) for s in manifest.symbols]}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _parse(payload: dict) -> Manifest:
    symbols = tuple(
        DeprecatedSymbol(
            symbol=s["symbol"],
            kind=s["kind"],
            module=s["module"],
            marker_via=s["marker_via"],
            message=s["message"],
            migration_target=s["migration_target"],
            removal_version=s.get("removal_version"),
        )
        for s in payload.get("symbols", [])
    )
    return Manifest(symbols=symbols)


def load_manifest(path: Path | None = None) -> Manifest:
    """Load the committed manifest.

    With *path* ``None`` (the normal B001 path) the manifest is read from the
    ``conformance`` package resource — robust across editable, wheel, and
    zip-imported layouts.  A *path* override is used by tests.

    A genuinely **absent** manifest yields an empty result silently (graceful
    degradation: an older wheel without the file simply produces no B001
    findings).  A **malformed** manifest is different — that is a packaging bug
    that would otherwise make B001 silently blind fleet-wide — so it is reported
    to stderr before degrading to empty, making the failure observable.
    """
    try:
        if path is None:
            text = (
                _ir.files("conformance")
                .joinpath(*_MANIFEST_RELPATH)
                .read_text(encoding="utf-8")
            )
        else:
            text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Manifest(symbols=())
    except OSError as exc:  # pragma: no cover - unusual IO failure
        print(f"warning: could not read deprecation manifest: {exc}", file=sys.stderr)
        return Manifest(symbols=())
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        print(
            f"warning: deprecation manifest is malformed JSON ({exc}); "
            "B001 deprecated-symbol detection is disabled until it is regenerated "
            "(`atlan-application-sdk-conformance gen-deprecations`).",
            file=sys.stderr,
        )
        return Manifest(symbols=())
    return _parse(payload)
