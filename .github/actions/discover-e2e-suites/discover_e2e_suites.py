"""Discover a connector's e2e test files and emit a GitHub Actions matrix.

The full-DAG e2e job fans out one matrix leg per test *file* under the
connector's e2e directory (default ``tests/e2e/``), so independent suites run
as separate jobs — parallel, with live per-job logs, per-suite re-run, and
isolation. This driver globs the files and prints the three outputs the workflow
consumes:

  matrix    — a JSON object ``{"include": [{"file": ..., "name": ...}, ...]}``
              suitable for ``strategy.matrix``. ``name`` is a sanitized, unique
              label used for the job name and the per-leg artifact suffix (so
              upload-artifact names don't collide across legs).
  count     — number of discovered *suites* (0 ⇒ the e2e job is skipped). This
              stays the suite count, not the leg count: the caller's "requested
              but nothing found" guard is about suites, and a cloud fan-out over
              zero suites is still zero suites.
  leg-count — number of matrix legs actually emitted (suites × clouds).

Cross-CSP fan-out (FND-6)
-------------------------
``--clouds aws,azure,gcp`` crosses every discovered suite with a cloud
provider, so each suite runs against one tenant per CSP and cloud-specific
nuances (the objectstore binding the configurator emits, blobstorage proxy
behaviour, Temporal host resolution) are exercised before release. The cloud
lands in the leg ``name``, which the caller threads into the job name, the
concurrency group, the artifact suffix, and — via ``derive_deployment_name.py``
— ``ATLAN_DEPLOYMENT_NAME``, so legs stay isolated on all four axes for free.

Three ``--clouds`` values, and the reason the empty one is not "no clouds":

* ``aws,azure,gcp`` — an explicit list.
* ``""`` — the default list, :data:`DEFAULT_CLOUDS`. Every app repo's
  ``tests.yaml`` forwards an operator-supplied ``e2e_clouds`` dispatch input
  straight through, and a GitHub input the operator left alone arrives as ``""``.
  Were that "no clouds", the entire fleet would silently opt out of the matrix
  the day the input was scaffolded. Making it mean "whatever the SDK currently
  ships" also keeps the list defined in exactly one place: adding a fourth CSP
  is an edit here, not in fifteen app repos.
* ``none`` — no cloud dimension. Reproduces the pre-FND-6 output byte for byte,
  for a caller without the tenant-matrix secret, or an operator deliberately
  falling back to the single legacy tenant.

``--clouds-only`` emits the cloud dimension *without* the file dimension, for
callers that target a whole directory rather than fanning out per file
(``e2e-full-reusable.yaml``).

Co-located with the composite action — NOT under ``.github/scripts/`` — so it
is checked out alongside the action when consumed from another repo (mirrors
build_compose_chain.py). It scans the *caller's* checked-out working tree, so
the SDK never needs to know a connector's specific suites.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SANITIZE_RE = re.compile(r"[^a-z0-9]+")

# The canonical cross-CSP fan-out. One tenant per cloud, per FND-6. Defined here
# rather than as a workflow-input default so a fourth CSP is one edit, not one
# per consumer repo — see the module docstring on why "" means this list.
DEFAULT_CLOUDS = ("aws", "azure", "gcp")

# Opt out of the cloud dimension entirely. A word rather than "" because "" is
# what an untouched GitHub input sends, and silently disabling the matrix on
# every un-customised run is the failure this sentinel exists to prevent.
NO_CLOUDS = "none"


def _leg_name(path: Path) -> str:
    """Derive a stable, filesystem-safe leg label from a test file path.

    ``tests/e2e/test_openapi_reuse_e2e.py`` -> ``openapi-reuse-e2e``. The
    ``test_`` prefix and ``.py`` suffix are stripped; anything else is lowercased
    and hyphenated so it is safe in a job name and an artifact suffix.
    """
    stem = path.stem
    if stem.startswith("test_"):
        stem = stem[len("test_") :]
    return _SANITIZE_RE.sub("-", stem.lower()).strip("-") or "e2e"


def parse_clouds(raw: str) -> list[str]:
    """Return the ordered, de-duplicated cloud list for a ``--clouds`` value.

    Accepts the comma-separated form the workflow input carries. ``""`` yields
    :data:`DEFAULT_CLOUDS` and ``"none"`` yields no clouds — see the module
    docstring for why round that way. Blank entries inside a list are dropped so
    a trailing comma is a no-op rather than a leg named "". Order is the
    caller's, not sorted: the operator writes ``aws,azure,gcp`` and the legs
    should read in that order.
    """
    stripped = raw.strip()
    if not stripped:
        return list(DEFAULT_CLOUDS)
    if stripped.lower() == NO_CLOUDS:
        return []

    out: list[str] = []
    for token in raw.split(","):
        cloud = _SANITIZE_RE.sub("-", token.strip().lower()).strip("-")
        if cloud and cloud not in out:
            out.append(cloud)
    return out


def discover(test_dir: str, clouds: list[str] | None = None) -> list[dict[str, str]]:
    """Return the ordered matrix ``include`` entries for *test_dir*.

    One entry per ``test_*.py`` directly under *test_dir*, crossed with *clouds*
    when given. Sorted for a stable leg order. Leg names are de-duplicated
    defensively (two files sanitizing to the same label get a numeric suffix) so
    artifact names stay unique.

    Suites are the outer loop and clouds the inner one, so the legs read as
    "suite A on each cloud, then suite B on each cloud" — the order an operator
    scans when one suite is failing everywhere versus one cloud failing
    everywhere.

    With no *clouds* the entries keep the pre-FND-6 ``{file, name}`` shape
    exactly: no ``suite``/``cloud`` keys are added, so ``matrix.cloud`` is empty
    in the caller and the tenant resolver takes its single-tenant fallback path.
    """
    root = Path(test_dir)
    files = sorted(p for p in root.glob("test_*.py") if p.is_file())

    suites: list[tuple[Path, str]] = []
    seen: dict[str, int] = {}
    for path in files:
        name = _leg_name(path)
        if name in seen:
            seen[name] += 1
            name = f"{name}-{seen[name]}"
        else:
            seen[name] = 1
        suites.append((path, name))

    if not clouds:
        return [{"file": path.as_posix(), "name": name} for path, name in suites]

    return [
        {
            "file": path.as_posix(),
            "suite": name,
            "cloud": cloud,
            "name": f"{name}-{cloud}",
        }
        for path, name in suites
        for cloud in clouds
    ]


def _nested_only(test_dir: str) -> list[Path]:
    """test_*.py found recursively but NOT by the flat (documented) glob.

    Discovery matches the documented flat ``tests/e2e/test_*.py`` layout only;
    a suite dropped into a subdirectory (e.g. during a migration) would run
    under a plain ``pytest tests/e2e`` but be silently absent from the matrix.
    Surfacing these lets the operator catch the drop instead of a silent green.
    """
    root = Path(test_dir)
    flat = {p.resolve() for p in root.glob("test_*.py") if p.is_file()}
    nested = {p.resolve() for p in root.rglob("test_*.py") if p.is_file()}
    return sorted(nested - flat)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover e2e suites for a matrix.")
    parser.add_argument("--test-dir", default="tests/e2e")
    parser.add_argument(
        "--clouds",
        default="",
        help=(
            "Comma-separated cloud providers to cross every discovered suite "
            f"with (e.g. aws,azure,gcp). Empty = the default list "
            f"({','.join(DEFAULT_CLOUDS)}); '{NO_CLOUDS}' = no cloud dimension, "
            "which reproduces the pre-FND-6 single-tenant matrix shape."
        ),
    )
    parser.add_argument(
        "--clouds-only",
        action="store_true",
        help=(
            "Emit only the cloud dimension (no file dimension), for callers "
            "that pass a whole directory to pytest instead of fanning out per "
            "suite. --test-dir is not read in this mode."
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    clouds = parse_clouds(args.clouds)

    if args.clouds_only:
        # No suites to count in this mode: the caller runs one pytest target per
        # cloud, so the suite count IS the cloud count and a zero there means
        # "no clouds configured" — which the caller's guard should still catch.
        entries = [{"cloud": cloud, "name": cloud} for cloud in clouds]
        print(
            f"Cloud-only matrix: {len(entries)} leg(s) [{', '.join(clouds) or 'none'}]",
            file=sys.stderr,
        )
        matrix = json.dumps({"include": entries}, separators=(",", ":"))
        print(f"matrix={matrix}")
        print(f"count={len(entries)}")
        print(f"leg-count={len(entries)}")
        return 0

    suites = discover(args.test_dir)
    entries = discover(args.test_dir, clouds)

    nested = _nested_only(args.test_dir)
    if nested:
        names = ", ".join(p.name for p in nested)
        print(
            f"::warning::{len(nested)} nested e2e test file(s) under {args.test_dir} "
            f"are NOT in the matrix — discovery matches the flat "
            f"tests/e2e/test_*.py convention only, so these would run under a "
            f"plain `pytest {args.test_dir}` but are skipped here: {names}",
            file=sys.stderr,
        )

    matrix = json.dumps({"include": entries}, separators=(",", ":"))
    # State the fan-out explicitly. A silent "4 suites" line when 12 legs are
    # about to run (or when the cloud list quietly collapsed to one) reads as
    # full coverage either way.
    print(
        f"Discovered {len(suites)} e2e suite(s) in {args.test_dir} × "
        f"{len(clouds) or 1} cloud(s) [{', '.join(clouds) or 'default tenant'}] "
        f"= {len(entries)} leg(s)",
        file=sys.stderr,
    )
    for e in entries:
        print(f"  - {e['name']}: {e['file']}", file=sys.stderr)
    print(f"matrix={matrix}")
    print(f"count={len(suites)}")
    print(f"leg-count={len(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
