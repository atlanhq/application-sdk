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
  clouds    — the RESOLVED cloud list, comma-separated ("" for no cloud
              dimension). The matrix already carries it per leg, but only a
              consumer that parses the matrix can see it, and "which clouds did
              this repo actually run against" is a fact the test-readiness
              scorecard records descriptively (FND-34). Emitting it here means
              the answer comes from the same ``parse_clouds`` call that decided
              the fan-out, so the recorded coverage cannot disagree with the
              coverage that ran.

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

Defaulted narrows, named does not (FND-354)
-------------------------------------------
``--available-clouds`` carries the cloud KEYS that ``E2E_TENANT_MATRIX_JSON``
actually holds — the key list, never the blob; the credentials stay in
``resolve_e2e_tenant.py``, which is the only thing that needs them. The two
``--clouds`` forms treat it differently, and the difference is the point:

* **defaulted** (``""``) → ``DEFAULT_CLOUDS ∩ available``, with a ``::warning::``
  naming every dropped cloud. Nobody asked for that cloud; ``DEFAULT_CLOUDS``
  did. Before FND-354 the defaulted list was emitted whole, so removing a cloud
  from the secret — the one lever that is fleet-wide and needs no PR — made
  ``resolve_e2e_tenant.py`` hard-fail that leg in *every* e2e-running repo
  instead of narrowing the fan-out. The obvious incident hatch did the opposite
  of what an operator reaches for it to do. It now narrows, out loud.
* **named** (``--clouds aws,azure``) → passed through untouched, so a cloud that
  is absent from the secret still reaches the resolver and still exits non-zero.
  Someone asserted that cloud should run; skipping it silently really would be a
  coverage hole.

Intersection only, never union: a fourth key appearing in the secret does not
widen the fleet's fan-out behind ``DEFAULT_CLOUDS``'s back. Widening stays a
deliberate edit here.

``--clouds-only`` emits the cloud dimension *without* the file dimension, for
callers that target a whole directory rather than fanning out per file
(``e2e-full-reusable.yaml``).

Per-suite dimensions (FND-1865)
-------------------------------
Two of the values a leg runs with used to be resolved once for the whole repo
while the legs were already per suite: ``source-available`` and the compose
overlay. A connector whose entrypoints differ in source provisioning could not
express that — db2's LUW flavour has a community container, its z/OS flavour
cannot have one at all (container images are architecture *and* OS specific) —
and no app-side workaround exists: the harness's class attribute loses to
``E2E_SOURCE_AVAILABLE`` on every CI run, and a module-level ``pytest.skip``
exits 5, which the composite propagates verbatim, so the leg reds rather than
skipping.

Both are resolved here, keyed off the discovered suite, and carried in the
matrix the same way the artifact suffix and the derived deployment name already
are — see :class:`PerSuiteOptions`:

* ``--source-available`` is the repo-wide default; ``--source-available-overrides``
  is ``<suite>=true|false``, comma-separated, and overrides in both directions.
  An override naming an undiscovered suite is a hard failure, not a no-op: an
  inert override leaves the leg on the repo-wide default, and the run greens
  having tried to extract from a source that cannot exist.
* ``--compose-overlay`` is the repo-wide FALLBACK; the per-suite overlay is a
  convention resolved against the caller's tree
  (``<dir>/<suite>-docker-compose.yaml``), which is possible here precisely
  because this driver runs after the caller's checkout.

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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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

# The per-suite compose-overlay convention (FND-1865). The matrix fans out one
# leg per suite, but the overlay used to be a single repo-wide path pinned by
# the caller, so a multi-entrypoint connector started EVERY source container on
# EVERY leg — and the worker's `depends_on: service_healthy` made the legs that
# cannot use one wait for it. A file named for the discovered suite wins over
# the repo-wide fallback; no such file means "nothing suite-specific here",
# which is what every single-flavour connector already has, so their behaviour
# stays byte-identical.
SUITE_COMPOSE_OVERLAY = "{suite}-docker-compose.yaml"

# The only two spellings a source-availability value may take here. The harness
# itself is lenient (``1``/``yes`` count as true) because it reads an env var
# several things write; this reads a hand-written workflow input, where
# rejecting a ``zos=0`` costs one loud discovery failure and accepting it
# silently decides whether a leg extracts anything at all.
_BOOLS = {"true": True, "false": False}


class CloudSelectionError(ValueError):
    """The requested cloud fan-out cannot be satisfied."""


class SuiteOptionError(ValueError):
    """A per-suite option is malformed, or names a suite that was not found."""


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


def _split_clouds(raw: str) -> list[str]:
    """Split a comma-separated cloud list into sanitized, de-duplicated keys.

    Blank entries are dropped so a trailing comma is a no-op rather than a leg
    named "". Order is the caller's, not sorted: the operator writes
    ``aws,azure,gcp`` and the legs should read in that order.
    """
    out: list[str] = []
    for token in raw.split(","):
        cloud = _SANITIZE_RE.sub("-", token.strip().lower()).strip("-")
        if cloud and cloud not in out:
            out.append(cloud)
    return out


def _defaulted_clouds(available: Iterable[str] | None) -> list[str]:
    """Return :data:`DEFAULT_CLOUDS` narrowed to what the tenant secret carries.

    *available* is the tenant matrix's key list. Empty or ``None`` means "not
    known here" — no secret shared with this repo, or a payload that could not
    be read — and narrowing is then skipped entirely, which is exactly the
    pre-FND-354 behaviour.

    Narrowing is an intersection, never a union: a key in the secret that is not
    in :data:`DEFAULT_CLOUDS` does not widen the fan-out. Removing a cloud from
    the secret is meant to be the fleet-wide incident hatch; adding one is a
    coverage decision that belongs in this file, where it is reviewed.
    """
    have = set(_split_clouds(",".join(available or ())))
    if not have:
        return list(DEFAULT_CLOUDS)

    kept = [cloud for cloud in DEFAULT_CLOUDS if cloud in have]
    dropped = [cloud for cloud in DEFAULT_CLOUDS if cloud not in have]
    if not kept:
        raise CloudSelectionError(
            "E2E_TENANT_MATRIX_JSON carries no cloud from the SDK's default "
            f"fan-out (default: {', '.join(DEFAULT_CLOUDS)}; secret: "
            f"{', '.join(sorted(have))}). Narrowing to nothing would emit zero "
            "legs and green the gate having run no e2e at all, so this fails "
            "instead. Restore a default cloud's entry in the secret, or pass "
            "e2e-clouds explicitly to run the clouds the secret does carry."
        )
    if dropped:
        print(
            "::warning::Cloud fan-out narrowed to what E2E_TENANT_MATRIX_JSON "
            f"carries: running {', '.join(kept)}; dropped "
            f"{', '.join(dropped)} (in the SDK default list, absent from the "
            "secret). This run has LESS cross-CSP coverage than the SDK ships. "
            "Nothing named these clouds — the default list did — so they are "
            "narrowed rather than failed; name a cloud in e2e-clouds to make "
            "its absence a hard failure instead.",
            file=sys.stderr,
        )
    return kept


def parse_clouds(raw: str, available: Iterable[str] | None = None) -> list[str]:
    """Return the ordered, de-duplicated cloud list for a ``--clouds`` value.

    Accepts the comma-separated form the workflow input carries. ``""`` yields
    :data:`DEFAULT_CLOUDS` and ``"none"`` yields no clouds — see the module
    docstring for why round that way.

    *available* (the tenant matrix's cloud keys) narrows the **defaulted** list
    only. An explicitly named cloud is passed through even when it is absent
    from *available*, so the per-leg resolver still hard-fails on it. That
    asymmetry is the whole of FND-354 and is pinned by
    ``test_a_named_absent_cloud_still_reaches_the_resolver``.

    Raises :class:`CloudSelectionError` when narrowing would leave no clouds at
    all.
    """
    stripped = raw.strip()
    if not stripped:
        return _defaulted_clouds(available)
    if stripped.lower() == NO_CLOUDS:
        return []

    return _split_clouds(raw)


def _parse_bool(raw: str, what: str) -> bool:
    """Return the boolean *raw* spells, or raise naming *what* and the value."""
    value = raw.strip().lower()
    if value not in _BOOLS:
        raise SuiteOptionError(
            f"{what} must be 'true' or 'false', not {raw.strip()!r}. Rejected "
            "rather than coerced: this value decides whether a leg runs the "
            "full DAG or degrades to a worker-up-only check, and a typo that "
            "read as false would green a leg that extracted nothing."
        )
    return _BOOLS[value]


def parse_source_available_overrides(raw: str) -> dict[str, bool]:
    """Parse the per-suite source-availability overrides (FND-1865).

    Accepts the comma-separated ``<suite>=true|false`` form the workflow input
    carries — ``"db2zos-e2e=false"`` — keyed off the DISCOVERED suite name (the
    matrix's ``suite``/``name``, i.e. ``test_db2zos_e2e.py`` -> ``db2zos-e2e``),
    which is the same key ``artifact-suffix`` and the derived
    ``ATLAN_DEPLOYMENT_NAME`` already use. ``""`` (an untouched input) yields no
    overrides, so every suite takes the repo-wide default.

    Overrides key on the SUITE, never on the leg: source availability is a
    property of the entrypoint, not of the CSP tenant the leg runs against, so
    an override applies to that suite on every cloud.

    Malformed tokens raise rather than being skipped — a dropped override is
    exactly the silent full-DAG-on-a-sourceless-leg the input exists to prevent.
    A suite named twice raises too, even with the same value: one of the two
    lines is not what its author meant.
    """
    overrides: dict[str, bool] = {}
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        suite, sep, value = item.partition("=")
        suite = suite.strip()
        if not sep or not suite:
            raise SuiteOptionError(
                f"source-available override {item!r} is not '<suite>=true|false'. "
                "The suite is the discovered suite name (test_db2zos_e2e.py -> "
                "db2zos-e2e), e.g. 'db2zos-e2e=false'."
            )
        if suite in overrides:
            raise SuiteOptionError(
                f"suite {suite!r} appears twice in the source-available "
                "overrides; one of the two is not what it meant to say."
            )
        overrides[suite] = _parse_bool(value, f"source-available override {suite!r}")
    return overrides


def resolve_compose_overlay(suite: str, fallback: str) -> str:
    """Return *suite*'s compose overlay: its own file if it exists, else *fallback*.

    The convention is ``<fallback's dir>/<suite>-docker-compose.yaml``, resolved
    against the CALLER's checked-out tree (this driver runs after the caller's
    checkout, which is what lets a per-suite file be a convention rather than an
    input). *fallback* is the single repo-wide overlay the caller used to pin for
    every leg, so a connector with no per-suite file is unaffected.

    Note the asymmetry with the source-availability default: a missing per-suite
    file falls back, it does not mean "no overlay". Expressing "this suite must
    layer nothing" is the app's job — it moves the shared overlay's contents
    into the per-suite files that want them and stops shipping the shared path.
    """
    candidate = Path(fallback).parent / SUITE_COMPOSE_OVERLAY.format(suite=suite)
    return candidate.as_posix() if candidate.is_file() else fallback


@dataclass(frozen=True)
class PerSuiteOptions:
    """The per-suite dimensions a matrix leg carries beyond file/name/cloud.

    Both existed as ONE repo-wide value per run while the legs were already
    per-suite (FND-1865): a multi-entrypoint connector whose entrypoints differ
    in source provisioning — db2's containerisable LUW flavour beside z/OS,
    which no container can serve — could not express that, and had no app-side
    workaround (a class attribute loses to the env var; a module-level
    ``pytest.skip`` exits 5 and reds the leg).

    ``compose_overlay`` is the repo-wide FALLBACK path, not the resolved one:
    resolution is per suite, against the caller's tree, in
    :func:`resolve_compose_overlay`.
    """

    source_available: bool = True
    source_available_overrides: Mapping[str, bool] = field(default_factory=dict)
    compose_overlay: str = ""

    def source_available_for(self, suite: str) -> bool:
        """Whether *suite* has a source: its override if it has one, else the default."""
        return self.source_available_overrides.get(suite, self.source_available)

    def keys_for(self, suite: str) -> dict[str, str]:
        """The extra matrix keys for *suite*, as the strings a leg forwards.

        ``source-available`` is always emitted — it always has a value, and a
        leg that forwarded an empty one would fall back to the harness's class
        default (true), which is the wrong direction to fail in for a connector
        that set it false. ``compose-overlay`` is emitted only when a fallback
        was supplied, because there is no sane default overlay path to derive a
        convention from (the SDR and full-DAG pipelines use different ones), and
        an empty value would send the sdr-e2e action to its OWN convention —
        the SDR overlay — on the full-DAG pipeline.
        """
        keys = {"source-available": str(self.source_available_for(suite)).lower()}
        if self.compose_overlay:
            keys["compose-overlay"] = resolve_compose_overlay(
                suite, self.compose_overlay
            )
        return keys

    def require_known_suites(self, suites: Iterable[str]) -> None:
        """Raise when an override names a suite discovery did not find.

        A typo'd or renamed suite name would otherwise be a no-op: the override
        matches nothing, the leg keeps the repo-wide default, and the run reports
        green having tried to extract from a source that does not exist. Failing
        in the discovery job costs seconds and names the suites that do exist.
        """
        known = list(suites)
        unknown = [s for s in self.source_available_overrides if s not in known]
        if unknown:
            raise SuiteOptionError(
                "source-available override(s) name suite(s) that were not "
                f"discovered: {', '.join(sorted(unknown))}. Discovered suites: "
                f"{', '.join(known) or 'none'}. An override that matches no "
                "suite is silently inert — the leg would keep the repo-wide "
                "default — so this fails instead."
            )


def discover(
    test_dir: str,
    clouds: list[str] | None = None,
    options: PerSuiteOptions | None = None,
) -> list[dict[str, str]]:
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

    *options* adds the per-suite dimensions (FND-1865) — ``source-available``,
    and ``compose-overlay`` when a fallback path was given. Omitted (the
    default) it adds nothing, so a caller that does not pass the new inputs gets
    the pre-FND-1865 entry shape unchanged.
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

    def extra(suite: str) -> dict[str, str]:
        return options.keys_for(suite) if options else {}

    if not clouds:
        return [
            {"file": path.as_posix(), "name": name, **extra(name)}
            for path, name in suites
        ]

    return [
        {
            "file": path.as_posix(),
            "suite": name,
            "cloud": cloud,
            "name": f"{name}-{cloud}",
            **extra(name),
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
        "--available-clouds",
        default="",
        help=(
            "Comma-separated cloud keys that E2E_TENANT_MATRIX_JSON actually "
            "carries — the KEY LIST, never the payload. Narrows the DEFAULTED "
            "--clouds list (and warns about each cloud it drops) so removing a "
            "cloud from the secret takes it out of the rotation fleet-wide "
            "instead of reding a leg in every repo. An explicitly named cloud "
            "is never narrowed. Empty = unknown; nothing is narrowed."
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
    parser.add_argument(
        "--source-available",
        default="true",
        help=(
            "The repo-wide source-availability default every discovered suite "
            "takes unless --source-available-overrides names it. 'true' (the "
            "default) runs the full DAG; 'false' degrades every leg to a "
            "worker-up-only check."
        ),
    )
    parser.add_argument(
        "--source-available-overrides",
        default="",
        help=(
            "Comma-separated <suite>=true|false overrides of the repo-wide "
            "default, keyed off the discovered suite name (test_db2zos_e2e.py "
            "-> db2zos-e2e), e.g. 'db2zos-e2e=false'. For a multi-entrypoint "
            "connector whose entrypoints are not equally testable. Empty = no "
            "overrides. An override naming an undiscovered suite is an error, "
            "not a no-op."
        ),
    )
    parser.add_argument(
        "--compose-overlay",
        default="",
        help=(
            "The repo-wide compose overlay each leg falls back to. When set, "
            "every leg's overlay is resolved per suite as "
            f"<dir>/{SUITE_COMPOSE_OVERLAY.format(suite='<suite>')} when that "
            "file exists in the caller's tree, else this path. Empty (the "
            "default) omits the compose-overlay matrix key entirely."
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        clouds = parse_clouds(args.clouds, _split_clouds(args.available_clouds))
    except CloudSelectionError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    # Parsed before the matrix is built, and nothing is written to stdout on the
    # failing path: a caller that read a matrix from an errored run would fan out
    # legs whose per-suite dimensions were never resolved.
    try:
        options = PerSuiteOptions(
            source_available=_parse_bool(args.source_available, "--source-available"),
            source_available_overrides=parse_source_available_overrides(
                args.source_available_overrides
            ),
            compose_overlay=args.compose_overlay.strip(),
        )
    except SuiteOptionError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if args.clouds_only:
        # The cloud-only mode has no suite dimension, so a per-suite option
        # passed here would be silently inert — which is the failure class the
        # whole of FND-1865 is about. Say so instead.
        if options.source_available_overrides or options.compose_overlay:
            print(
                "::error::--source-available-overrides / --compose-overlay have "
                "no meaning with --clouds-only: that mode emits no suite "
                "dimension, so a per-suite value could not reach any leg. Drop "
                "them, or drop --clouds-only.",
                file=sys.stderr,
            )
            return 1

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
        print(f"clouds={','.join(clouds)}")
        return 0

    suites = discover(args.test_dir)
    try:
        options.require_known_suites(e["name"] for e in suites)
    except SuiteOptionError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    entries = discover(args.test_dir, clouds, options)

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
    # Per-leg, not per-run: "source-available: false" printed once for the run
    # is what this driver used to be able to say, and it is exactly the sentence
    # that was wrong for a connector with one testable flavour and one not.
    for e in entries:
        dims = "".join(
            f" {key}={e[key]}"
            for key in ("source-available", "compose-overlay")
            if key in e
        )
        print(f"  - {e['name']}: {e['file']}{dims}", file=sys.stderr)
    print(f"matrix={matrix}")
    print(f"count={len(suites)}")
    print(f"leg-count={len(entries)}")
    # Empty means "no cloud dimension" — the legacy single-tenant fallback — and
    # the consumer must be able to tell that apart from "e2e never ran", which is
    # why the scorecard omits the field entirely in the latter case rather than
    # recording an empty list.
    print(f"clouds={','.join(clouds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
