"""
Select and layer the Dapr CI components used by the sdr-e2e composite action.

Mirrors what the "Override CI Dapr components" step in
``.github/actions/sdr-e2e/action.yaml`` used to do inline: copy the SDK's
default component set into ``ci-deploy/components/``, then layer the app's
own overrides on top so a connector can replace any single component file
without forking the whole set.

Two-store mode (ADR-0014, docs/adr/0014-two-store-storage-architecture.md)
adds one twist: the connector's own ``objectstore.yaml`` override — in the
full-DAG pipeline this has historically pointed ``objectstore`` directly at
the tenant blobstorage proxy — is skipped, so the SDK-default localstorage
binding always wins. That is what keeps the deployment store
(``objectstore``) and the upstream store (``atlan-objectstore``, emitted by
the atlan-configurator from the ``atlan:`` block in test-config.yaml)
physically distinct. Without that split, a connector that forgets to bridge
an artifact via ``App.upload()`` would still have it land in the one shared
bucket the downstream system apps read, and the gap would go unnoticed by
the full-DAG e2e suite.

In two-store mode this also hard-asserts that the configurator actually
emitted ``atlan-objectstore.yaml``, failing fast with an actionable message
instead of leaving the run to hit a boot-time storage preflight failure
much later in the pipeline.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# The one component two-store mode refuses to let an app override — see
# module docstring. A tuple (not a single constant) so a future component
# needing the same treatment is an explicit, reviewed addition.
_TWO_STORE_PROTECTED_COMPONENTS = ("objectstore.yaml",)

_UPSTREAM_COMPONENT = "atlan-objectstore.yaml"


class MissingUpstreamComponentError(RuntimeError):
    """Two-store mode is on but the configurator never emitted atlan-objectstore.yaml."""


def _resolve_app_components_dir(
    app_components_dir: str, sdr_config_dir: str
) -> Path | None:
    """Resolve the effective app-override directory from raw CLI strings.

    Mirrors the bash fallback this replaces: an explicit ``app_components_dir``
    wins; otherwise fall back to the ``<sdr_config_dir>/components`` convention;
    otherwise there is no app-override directory at all. Both inputs may be
    empty strings (GitHub Actions passes "" for an unset input), which is
    treated the same as "not provided".
    """
    if app_components_dir:
        return Path(app_components_dir)
    if sdr_config_dir:
        return Path(sdr_config_dir) / "components"
    return None


def select_components(
    ci_components_dir: Path,
    sdk_components_dir: Path,
    app_components_dir: Path | None,
    *,
    two_store: bool,
) -> list[str]:
    """Populate *ci_components_dir* with the effective component set.

    Copies every SDK-default component, then layers non-skipped app
    overrides on top (existing files with the same name are replaced).
    Files already present in *ci_components_dir* that aren't touched here
    (e.g. the configurator's own generated components) are left alone.

    Returns the sorted list of app-override filenames skipped because
    two-store mode protects them (empty unless that happened).

    Raises ``MissingUpstreamComponentError`` if two-store mode is on and
    *ci_components_dir* doesn't end up with an ``atlan-objectstore.yaml``.
    """
    ci_components_dir.mkdir(parents=True, exist_ok=True)

    for component in sorted(sdk_components_dir.glob("*.yaml")):
        shutil.copy2(component, ci_components_dir / component.name)

    skipped: list[str] = []
    if app_components_dir is not None and app_components_dir.is_dir():
        for component in sorted(app_components_dir.glob("*.yaml")):
            if two_store and component.name in _TWO_STORE_PROTECTED_COMPONENTS:
                skipped.append(component.name)
                continue
            shutil.copy2(component, ci_components_dir / component.name)

    if two_store and not (ci_components_dir / _UPSTREAM_COMPONENT).is_file():
        raise MissingUpstreamComponentError(
            f"two-store mode is enabled but {ci_components_dir / _UPSTREAM_COMPONENT} "
            "is missing. The atlan-configurator only emits this component when the "
            "resolved test-config.yaml carries an `atlan:` block (domain / client_id "
            "/ client_secret). If this app ships a custom test-config.yaml.tmpl for "
            "the full-DAG pipeline, make sure it still includes that block."
        )

    return sorted(skipped)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ci-components-dir", required=True, type=Path)
    parser.add_argument("--sdk-components-dir", required=True, type=Path)
    parser.add_argument(
        "--app-components-dir",
        default="",
        help="Explicit app-override dir (empty = fall back to --sdr-config-dir convention).",
    )
    parser.add_argument(
        "--sdr-config-dir",
        default="",
        help="Base SDR config dir; <this>/components is the override-dir convention.",
    )
    parser.add_argument(
        "--two-store",
        choices=("true", "false"),
        default="false",
        help="Enable the ADR-0014 two-store posture (default: false).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    app_dir = _resolve_app_components_dir(args.app_components_dir, args.sdr_config_dir)
    two_store = args.two_store == "true"

    try:
        skipped = select_components(
            args.ci_components_dir,
            args.sdk_components_dir,
            app_dir,
            two_store=two_store,
        )
    except MissingUpstreamComponentError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    for name in skipped:
        print(
            f"Skipping app override {name} — two-store mode keeps the "
            "SDK-default localstorage objectstore so the deployment and "
            "upstream stores stay physically distinct.",
            file=sys.stderr,
        )
    print(f"Effective components in {args.ci_components_dir}:", file=sys.stderr)
    for component in sorted(args.ci_components_dir.glob("*.yaml")):
        print(f"  {component.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
