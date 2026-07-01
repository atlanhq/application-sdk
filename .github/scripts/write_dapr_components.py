"""
Provision default local-backed Dapr components for the external-runtime CI path.

The ``connector-integration-tests`` action starts an external ``daprd`` with
``--resources-path components`` when a connector has NOT migrated to the SDK's
embedded runtime (``force-external-runtime: true``, or SDK < 3.13). That path
assumes the connector repo ships a ``components/`` directory declaring the Dapr
bindings the SDK resolves at startup (``objectstore``, ``statestore``,
``secretstore`` …). Almost no connector ships one, so ``daprd`` came up with no
``objectstore`` binding and app startup crashed with
``StorageBindingNotFoundError [AAF-STR-003] No Dapr component named
'objectstore'`` — breaking CI for every un-migrated connector.

This script writes the default local-backed component set (objectstore /
eventstore as ``bindings.localstorage``, statestore / pubsub in-memory,
secret stores as ``local.env``) into the target directory so the external
``daprd`` — and the SDK reading the same YAML off disk — find every binding.

Write-if-absent: a component file that already exists is left untouched, so a
connector (or the SDK's own real-cloud S3/Azure/GCS suite) that ships a real
``components/objectstore.yaml`` keeps it. Only missing components are filled in.

Source of truth for the templates is
``application_sdk/dev/_dapr.py::_COMPONENTS_YAML`` (the embedded runtime writes
the same set). They are duplicated here — not imported — so the action, pinned
at ``@main``, does not depend on the connector's installed SDK version. Keep
the two in sync; drift only risks CI provisioning, never production.

Usage::

    python .github/scripts/write_dapr_components.py [COMPONENTS_DIR] \\
        [--objectstore-root PATH] [--eventstore-root PATH]

``COMPONENTS_DIR`` defaults to ``components`` (matching the action's
``--resources-path components`` and the SDK's ``./components`` default).
Emits one ``wrote``/``kept`` line per component and exits 0; exits non-zero
with a ``::error::`` annotation only on an unexpected I/O failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Mirrors application_sdk/dev/_dapr.py::_COMPONENTS_YAML — see module docstring.
_COMPONENTS: dict[str, str] = {
    "statestore.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.in-memory
  version: v1
  metadata: []
""",
    "secretstore.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: secretstore
spec:
  type: secretstores.local.env
  version: v1
  metadata: []
""",
    "deployment-secret-store.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: deployment-secret-store
spec:
  type: secretstores.local.env
  version: v1
  metadata: []
""",
    "objectstore.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: objectstore
spec:
  type: bindings.localstorage
  version: v1
  metadata:
    - name: rootPath
      value: {objectstore_root}
""",
    "eventstore.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: eventstore
spec:
  type: bindings.localstorage
  version: v1
  metadata:
    - name: rootPath
      value: {eventstore_root}
""",
    "pubsub.yaml": """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: pubsub
spec:
  type: pubsub.in-memory
  version: v1
  metadata: []
""",
}

DEFAULT_OBJECTSTORE_ROOT = "./local/dapr/objectstore"
DEFAULT_EVENTSTORE_ROOT = "./local/dapr/eventstore"


def write_components(
    components_dir: Path,
    objectstore_root: Path = Path(DEFAULT_OBJECTSTORE_ROOT),
    eventstore_root: Path = Path(DEFAULT_EVENTSTORE_ROOT),
) -> dict[str, str]:
    """Write the default component YAMLs into *components_dir*, write-if-absent.

    Returns a mapping of ``filename -> "wrote" | "kept"`` describing what
    happened to each component, so the caller (and tests) can assert coverage.
    Existing files are never overwritten.
    """
    components_dir.mkdir(parents=True, exist_ok=True)
    objectstore_root.mkdir(parents=True, exist_ok=True)
    eventstore_root.mkdir(parents=True, exist_ok=True)

    result: dict[str, str] = {}
    for filename, template in _COMPONENTS.items():
        target = components_dir / filename
        if target.exists():
            result[filename] = "kept"
            continue
        target.write_text(
            template.format(
                objectstore_root=str(objectstore_root.resolve()),
                eventstore_root=str(eventstore_root.resolve()),
            )
        )
        result[filename] = "wrote"
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "components_dir",
        nargs="?",
        default="components",
        help="Directory to write component YAMLs into (default: components).",
    )
    parser.add_argument(
        "--objectstore-root",
        default=DEFAULT_OBJECTSTORE_ROOT,
        help=f"Local root path for the objectstore binding (default: {DEFAULT_OBJECTSTORE_ROOT}).",
    )
    parser.add_argument(
        "--eventstore-root",
        default=DEFAULT_EVENTSTORE_ROOT,
        help=f"Local root path for the eventstore binding (default: {DEFAULT_EVENTSTORE_ROOT}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = write_components(
            Path(args.components_dir),
            Path(args.objectstore_root),
            Path(args.eventstore_root),
        )
    except OSError as exc:
        print(f"::error::Failed to write Dapr components: {exc}", file=sys.stderr)
        return 1

    for filename, action in sorted(result.items()):
        print(f"{action}: {args.components_dir}/{filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
