"""The command line every committed-data generator shares.

Each generator (deprecation manifest, public-error allowlist, SDK type-alias
table, toolkit baseline, relationship table) reads something from the SDK repo,
serialises it deterministically, and either writes the committed file or, with
``--check``, fails when the committed file is stale. Only what is read and how
it is described differ, so each generator module declares a
:class:`GeneratorSpec` and calls :func:`run_generator`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Generated:
    """One generator run's output."""

    content: str
    """The serialised file, compared byte-for-byte with the committed one."""

    check_detail: str
    """Shown in brackets after ``... up-to-date``, e.g. ``"12 symbols"``."""

    write_detail: str
    """Shown in brackets after ``Wrote <file>``."""


@dataclass(frozen=True)
class GeneratorSpec:
    """What one generator reads, writes and calls itself."""

    doc: str
    """The generator module's docstring, shown as the ``--help`` epilog."""

    description: str
    command: str
    """The ``atlan-application-sdk-conformance`` subcommand that regenerates it."""

    outfile: Path
    noun: str
    """The committed file, lower case, for ``--help`` (``"manifest"``)."""

    title: str
    """The committed file, capitalised, for the up-to-date line (``"Manifest"``)."""

    build: Callable[[Path | None], Generated]
    """Builds the output. Receives the SDK repo root, or ``None`` when
    :attr:`sdk_marker` is ``None`` (the generator reads no SDK source)."""

    sdk_marker: tuple[str, ...] | None = None
    """Path parts of a file that proves a directory is the SDK repo root."""

    sdk_marker_label: str = ""
    """How :attr:`sdk_marker` is named in ``--help`` and the not-found error."""

    accepts_sdk_root: Callable[[Path], bool] | None = None
    """Decides whether an explicit ``--sdk-root`` is usable. Defaults to
    "holds :attr:`sdk_marker`". A generator sets it when it accepts a wider
    root than the marker auto-detection looks for."""


def _find_sdk_root(marker: tuple[str, ...]) -> Path | None:
    """Return the first ancestor of the working directory, then of this file,
    that holds *marker*."""
    for start in (Path.cwd(), Path(__file__).resolve()):
        for parent in (start, *start.parents):
            if parent.joinpath(*marker).is_file():
                return parent
    return None


def _parse_args(spec: GeneratorSpec, argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=spec.description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=spec.doc,
    )
    if spec.sdk_marker is not None:
        parser.add_argument(
            "--sdk-root",
            type=Path,
            default=None,
            help=f"Repo root containing {spec.sdk_marker_label} (default: auto-detected).",
        )
    parser.add_argument(
        "--outfile",
        type=Path,
        default=spec.outfile,
        help=f"{spec.noun.capitalize()} path to write (default: {spec.outfile}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=f"Verify the committed {spec.noun} matches generated output (exit 1 if stale).",
    )
    return parser.parse_args(argv)


def run_generator(spec: GeneratorSpec, argv: list[str] | None = None) -> None:
    """Parse *argv*, build the output, then write it or check it.

    Exits 2 when the SDK repo root cannot be found, and 1 when ``--check``
    finds the committed file missing or stale.
    """
    args = _parse_args(spec, argv)

    sdk_root: Path | None = None
    if spec.sdk_marker is not None:
        marker = spec.sdk_marker
        accepts = spec.accepts_sdk_root or (
            lambda root: root.joinpath(*marker).is_file()
        )
        sdk_root = args.sdk_root or _find_sdk_root(marker)
        if sdk_root is None or not accepts(sdk_root):
            print(
                f"error: could not locate {spec.sdk_marker_label} — pass --sdk-root DIR.",
                file=sys.stderr,
            )
            sys.exit(2)

    generated = spec.build(sdk_root)
    outfile: Path = args.outfile

    if args.check:
        if not outfile.exists():
            print(f"MISSING: {outfile}", file=sys.stderr)
            sys.exit(1)
        if outfile.read_text(encoding="utf-8") != generated.content:
            print(
                f"STALE: {outfile}\nRun `uv run atlan-application-sdk-conformance "
                f"{spec.command}` to update.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"{spec.title} up-to-date ({generated.check_detail}).")
        return

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(generated.content, encoding="utf-8")
    print(f"Wrote {outfile} ({generated.write_detail}).")
