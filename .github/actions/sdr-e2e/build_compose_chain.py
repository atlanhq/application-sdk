"""
Build the ``docker compose -f ...`` file chain for the sdr-e2e composite action.

Layering order (later files win on conflicting keys):

  1. atlan-configurator-generated base compose (``ci-deploy/docker-compose.yaml``)
  2. SDK CI overrides (``docker-compose.ci.yml``)
  3. full-DAG queue-alignment overlay (``docker-compose.full-dag.yml``), only when
     full-DAG mode is enabled — SDK-owned, replaces the byte-identical per-app
     ``ATLAN_DEPLOYMENT_NAME`` overlay. Below the app overlay so an app can still
     override it.
  4. optional app-level overlay (explicit ``compose-overlay`` input, else the
     ``<sdr-config-dir>/docker-compose.ci.yml`` convention)
  5. two-store overlay (``docker-compose.two-store.yml``), only when two-store
     mode is enabled — applied LAST so ``ENABLE_ATLAN_UPLOAD`` /
     ``ATLAN_DEPLOYMENT_ARTIFACT_DUAL_WRITE`` always win over anything an app
     overlay might set.

Mirrors the "Build compose -f chain" step in
``.github/actions/sdr-e2e/action.yaml`` — moved here (rather than left as
inline ``if`` shell) per ``docs/standards/ci.md``, which reserves
conditional logic in ``run:`` blocks for tested Python.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_compose_files(
    base_compose: Path,
    sdk_compose: Path,
    app_compose: str,
    sdr_config_dir: str,
    *,
    two_store: bool,
    two_store_compose: Path | None = None,
    full_dag: bool = False,
    full_dag_compose: Path | None = None,
) -> list[str]:
    """Return the ordered list of docker compose file paths to layer.

    *app_compose* / *sdr_config_dir* are raw strings (possibly empty) as
    they arrive from GitHub Actions inputs/env — empty means "not set",
    not "a file named ''".

    Raises ``ValueError`` if *two_store* is true but *two_store_compose*
    wasn't supplied, or if *full_dag* is true but *full_dag_compose* wasn't
    supplied — both overlays are SDK-owned and always expected to exist, so a
    missing path here is a caller bug, not a per-app condition.
    """
    files = [str(base_compose), str(sdk_compose)]

    if full_dag:
        if full_dag_compose is None:
            raise ValueError("full_dag_compose is required when full_dag=True")
        files.append(str(full_dag_compose))

    overlay = app_compose or (
        str(Path(sdr_config_dir) / "docker-compose.ci.yml") if sdr_config_dir else ""
    )
    if overlay and Path(overlay).is_file():
        files.append(overlay)

    if two_store:
        if two_store_compose is None:
            raise ValueError("two_store_compose is required when two_store=True")
        files.append(str(two_store_compose))

    return files


def _compose_flags(files: list[str]) -> str:
    flags: list[str] = []
    for f in files:
        flags.extend(("-f", f))
    return " ".join(flags)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-compose", required=True, type=Path)
    parser.add_argument("--sdk-compose", required=True, type=Path)
    parser.add_argument("--app-compose", default="")
    parser.add_argument("--sdr-config-dir", default="")
    parser.add_argument(
        "--two-store",
        choices=("true", "false"),
        default="false",
        help="Enable the ADR-0014 two-store posture (default: false).",
    )
    parser.add_argument(
        "--two-store-compose",
        type=Path,
        default=None,
        help="Path to docker-compose.two-store.yml; required when --two-store=true.",
    )
    parser.add_argument(
        "--full-dag",
        choices=("true", "false"),
        default="false",
        help="Enable the full-DAG queue-alignment overlay (default: false).",
    )
    parser.add_argument(
        "--full-dag-compose",
        type=Path,
        default=None,
        help="Path to docker-compose.full-dag.yml; required when --full-dag=true.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    two_store = args.two_store == "true"
    full_dag = args.full_dag == "true"

    try:
        files = build_compose_files(
            args.base_compose,
            args.sdk_compose,
            args.app_compose,
            args.sdr_config_dir,
            two_store=two_store,
            two_store_compose=args.two_store_compose,
            full_dag=full_dag,
            full_dag_compose=args.full_dag_compose,
        )
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    print(f"Effective compose chain: {' '.join(files)}", file=sys.stderr)
    if full_dag:
        print("full-DAG mode: inserted docker-compose.full-dag.yml", file=sys.stderr)
    if two_store:
        print("two-store mode: appended docker-compose.two-store.yml", file=sys.stderr)
    print(f"files={_compose_flags(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
