#!/usr/bin/env python3
"""Prove the api package's THIN base install still imports, builds and stays small.

The consolidated API host installs ``atlan-application-sdk-api`` with no capability
extras and mounts every hosted app in one process. Its test suite always has the
extras present, so a top-level ``import sqlalchemy`` (or a reach back into
``application_sdk``) would keep every test green and then fail at mount time on
the host -- taking every co-hosted app down -- or quietly bloat each pod.

Run this with the interpreter of a venv that has ONLY the base install::

    uv venv .venv-base && uv pip install --python .venv-base packages/api
    .venv-base/bin/python .github/scripts/probe_api_base_install.py --max-rss-mb 120

It imports every module (except those a capability extra deliberately gates),
builds an app, then fails on any unexpected import error, any heavy worker-side
module found in ``sys.modules``, an app with no routes, or a peak RSS above the
budget. Import+build time is reported, not asserted (too noisy on shared runners).
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import resource
import sys
import time
from dataclasses import dataclass, field

#: Modules a capability extra gates; they may fail to import on the base install.
GATED: frozenset[str] = frozenset({"application_sdk_api.workflow.temporal"})

#: Worker-side / heavy top-level packages the base install must never load.
FORBIDDEN: frozenset[str] = frozenset(
    {
        "application_sdk",
        "temporalio",
        "pyatlan",
        "obstore",
        "opentelemetry",
        "loguru",
        "daft",
        "duckdb",
        "pandas",
        "pyarrow",
        "sqlalchemy",
        "boto3",
    }
)


@dataclass
class Probe:
    """What the probe observed; :func:`problems` turns it into a verdict."""

    failed_imports: dict[str, str] = field(default_factory=dict)
    loaded_modules: frozenset[str] = frozenset()
    route_count: int = 0
    max_rss_mb: float = 0.0


def problems(probe: Probe, *, max_rss_mb: float) -> list[str]:
    """Every reason the base install is not fit to ship, empty when it is."""
    found: list[str] = []
    for name, err in sorted(probe.failed_imports.items()):
        if name not in GATED:
            found.append(f"import failed on the base install: {name} -> {err}")
    heavy = sorted(m for m in probe.loaded_modules if m.split(".", 1)[0] in FORBIDDEN)
    heavy_roots = sorted({m.split(".", 1)[0] for m in heavy})
    if heavy_roots:
        found.append(f"worker-side packages loaded by the base install: {heavy_roots}")
    if probe.route_count <= 0:
        found.append("build_asgi_app produced an app with no routes")
    if probe.max_rss_mb > max_rss_mb:
        found.append(
            f"peak RSS {probe.max_rss_mb:.1f} MB exceeds the {max_rss_mb:.0f} MB budget"
        )
    return found


def _peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes.
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def run() -> tuple[Probe, float]:
    """Import everything, build an app, and measure. Returns (probe, seconds)."""
    started = time.perf_counter()
    import application_sdk_api  # noqa: PLC0415 — timed: the import is what is measured

    failed: dict[str, str] = {}
    for mod in pkgutil.walk_packages(
        application_sdk_api.__path__, "application_sdk_api."
    ):
        try:
            importlib.import_module(mod.name)
        except Exception as exc:  # noqa: BLE001 — every failure is reported
            failed[mod.name] = f"{type(exc).__name__}: {exc}"

    from application_sdk_api import build_asgi_app  # noqa: PLC0415 — timed, see above
    from application_sdk_api.handler import DefaultHandler  # noqa: PLC0415 — timed

    app = build_asgi_app(DefaultHandler(), app_name="base-install-probe")
    routes = len(app.openapi()["paths"])
    elapsed = time.perf_counter() - started
    return (
        Probe(
            failed_imports=failed,
            loaded_modules=frozenset(sys.modules),
            route_count=routes,
            max_rss_mb=_peak_rss_mb(),
        ),
        elapsed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-rss-mb", type=float, default=120.0)
    args = parser.parse_args(argv)
    probe, elapsed = run()
    found = problems(probe, max_rss_mb=args.max_rss_mb)
    for line in found:
        print(f"BASE INSTALL: {line}", file=sys.stderr)
    print(
        f"base install: {probe.route_count} routes, peak RSS {probe.max_rss_mb:.1f} MB "
        f"(budget {args.max_rss_mb:.0f}), import+build {elapsed:.2f}s, "
        f"gated-out: {sorted(GATED & probe.failed_imports.keys())}"
    )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
