"""
Provision default local-backed Dapr components for CI.

Two callers, selected by ``--profile``:

``host`` (default) — the ``connector-integration-tests`` action. It starts an
external ``daprd`` with ``--resources-path components`` when a connector has NOT
migrated to the SDK's embedded runtime (``force-external-runtime: true``, or
SDK < 3.13). That path assumes the connector repo ships a ``components/``
directory declaring the Dapr bindings the SDK resolves at startup
(``objectstore``, ``statestore``, ``secretstore`` …). Almost no connector ships
one, so ``daprd`` came up with no ``objectstore`` binding and app startup
crashed with ``StorageBindingNotFoundError [AAF-STR-003] No Dapr component named
'objectstore'``. This profile writes the full default set (objectstore /
eventstore as ``bindings.localstorage``, statestore / pubsub in-memory, secret
stores as ``local.env``), with the binding roots created and resolved on this
host.

``sdr-e2e`` — the ``sdr-e2e`` action. It writes the SDK-default layer that
``select_dapr_components.py`` copies into ``ci-deploy/components/``, which the
app container mounts. Only ``objectstore``, ``eventstore`` and a file-backed
``secretstore`` are written; the configurator and app overrides own the rest.
The paths are container paths and are written verbatim (never resolved or
created on the runner): the binding roots live in the container's ``/tmp``, and
the secrets file is ``/app/secrets/credentials.json``, where
``docker-compose.ci.yml`` mounts ``<sdr-config-dir>/secrets`` and the action's
"Generate secrets bundle" step guarantees ``credentials.json`` exists.

Write-if-absent: a component file that already exists is left untouched, so a
connector (or the SDK's own real-cloud S3/Azure/GCS suite) that ships a real
``components/objectstore.yaml`` keeps it. Only missing components are filled in.

The templates are not defined here. They live in
``application_sdk/dev/_dapr_components.py``, which the SDK's embedded runtime
renders too, so local dev, connector CI and SDR e2e share one definition. That
module is loaded by file path from this checkout of the SDK (the action's own
ref), not imported from the connector's installed SDK, so the action does not
depend on the connector's SDK version and this script stays stdlib-only.

Usage::

    python .github/scripts/write_dapr_components.py [COMPONENTS_DIR] \\
        [--objectstore-root PATH] [--eventstore-root PATH]
    python .github/scripts/write_dapr_components.py COMPONENTS_DIR --profile sdr-e2e

``COMPONENTS_DIR`` defaults to ``components`` (matching the action's
``--resources-path components`` and the SDK's ``./components`` default).
Emits one ``wrote``/``kept`` line per component and exits 0; exits non-zero
with a ``::error::`` annotation only on an unexpected I/O failure.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

_SHARED_TEMPLATES = (
    Path(__file__).resolve().parents[2]
    / "application_sdk"
    / "dev"
    / "_dapr_components.py"
)


class _RenderComponents(Protocol):
    def __call__(
        self,
        *,
        objectstore_root: str,
        eventstore_root: str,
        secrets_file: str | None = None,
    ) -> dict[str, str]: ...


def _load_shared_templates() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_sdk_dapr_components", _SHARED_TEMPLATES
    )
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load Dapr component templates from {_SHARED_TEMPLATES}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shared = _load_shared_templates()
render_components = cast(_RenderComponents, _shared.render_components)
DEFAULT_OBJECTSTORE_ROOT = cast(str, _shared.DEFAULT_OBJECTSTORE_ROOT)
DEFAULT_EVENTSTORE_ROOT = cast(str, _shared.DEFAULT_EVENTSTORE_ROOT)

# sdr-e2e container layout — see module docstring.
SDR_E2E_OBJECTSTORE_ROOT = "/tmp/atlan-data"
SDR_E2E_EVENTSTORE_ROOT = "/tmp/atlan-eventstore"
SDR_E2E_SECRETS_FILE = "/app/secrets/credentials.json"
SDR_E2E_COMPONENTS = ("objectstore.yaml", "eventstore.yaml", "secretstore.yaml")


def _write_if_absent(components_dir: Path, rendered: dict[str, str]) -> dict[str, str]:
    components_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    for filename, content in rendered.items():
        target = components_dir / filename
        if target.exists():
            result[filename] = "kept"
            continue
        target.write_text(content, encoding="utf-8")
        result[filename] = "wrote"
    return result


def write_components(
    components_dir: Path,
    objectstore_root: Path = Path(DEFAULT_OBJECTSTORE_ROOT),
    eventstore_root: Path = Path(DEFAULT_EVENTSTORE_ROOT),
) -> dict[str, str]:
    """Write the full default set (``host`` profile), write-if-absent.

    The binding roots are created and resolved to absolute paths on this host.
    Returns a mapping of ``filename -> "wrote" | "kept"`` describing what
    happened to each component, so the caller (and tests) can assert coverage.
    Existing files are never overwritten.
    """
    objectstore_root.mkdir(parents=True, exist_ok=True)
    eventstore_root.mkdir(parents=True, exist_ok=True)
    rendered = render_components(
        objectstore_root=str(objectstore_root.resolve()),
        eventstore_root=str(eventstore_root.resolve()),
    )
    return _write_if_absent(components_dir, rendered)


def write_sdr_e2e_components(components_dir: Path) -> dict[str, str]:
    """Write the SDK-default layer for the sdr-e2e container, write-if-absent."""
    rendered = render_components(
        objectstore_root=SDR_E2E_OBJECTSTORE_ROOT,
        eventstore_root=SDR_E2E_EVENTSTORE_ROOT,
        secrets_file=SDR_E2E_SECRETS_FILE,
    )
    subset = {name: rendered[name] for name in SDR_E2E_COMPONENTS}
    return _write_if_absent(components_dir, subset)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "components_dir",
        nargs="?",
        default="components",
        help="Directory to write component YAMLs into (default: components).",
    )
    parser.add_argument(
        "--profile",
        choices=("host", "sdr-e2e"),
        default="host",
        help="Which component layout to write (default: host).",
    )
    parser.add_argument(
        "--objectstore-root",
        default=None,
        help=f"host profile: local root for the objectstore binding (default: {DEFAULT_OBJECTSTORE_ROOT}).",
    )
    parser.add_argument(
        "--eventstore-root",
        default=None,
        help=f"host profile: local root for the eventstore binding (default: {DEFAULT_EVENTSTORE_ROOT}).",
    )
    args = parser.parse_args(argv)
    if args.profile == "sdr-e2e" and (
        args.objectstore_root is not None or args.eventstore_root is not None
    ):
        parser.error(
            "--objectstore-root/--eventstore-root apply to the host profile only"
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    components_dir = Path(args.components_dir)
    try:
        if args.profile == "sdr-e2e":
            result = write_sdr_e2e_components(components_dir)
        else:
            result = write_components(
                components_dir,
                Path(args.objectstore_root or DEFAULT_OBJECTSTORE_ROOT),
                Path(args.eventstore_root or DEFAULT_EVENTSTORE_ROOT),
            )
    except OSError as exc:
        print(f"::error::Failed to write Dapr components: {exc}", file=sys.stderr)
        return 1

    for filename, action in sorted(result.items()):
        print(f"{action}: {args.components_dir}/{filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
