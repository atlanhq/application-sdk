"""Default local-backed Dapr component templates — the single source.

Rendered by:

* ``application_sdk.dev._dapr`` — the embedded ``daprd`` for local dev and the
  integration suite.
* ``.github/scripts/write_dapr_components.py`` — the external-runtime path of
  the ``connector-integration-tests`` action and the ``sdr-e2e`` action.

The CI script runs under the runner's system ``python3`` from the action's own
checkout of this repo, not from the connector's installed SDK, so it loads this
file by path. That is why this module must stay **stdlib-only** and must not
import anything from ``application_sdk``: an import here would break both CI
paths. ``tests/unit/dev/test_dapr_components.py`` enforces that.

The repo-root ``components/`` folder is a separate, wheel-shipped set (see
``components/README.md``); only its localstorage roots are tied to the
defaults below.
"""

from __future__ import annotations

DEFAULT_OBJECTSTORE_ROOT = "./local/dapr/objectstore"
DEFAULT_EVENTSTORE_ROOT = "./local/dapr/eventstore"

COMPONENT_TEMPLATES: dict[str, str] = {
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

# File-backed secret store, used in place of ``local.env`` when a secrets file
# path is supplied (integration tests that assert specific secret values, and
# the SDR e2e container, which mounts a credentials bundle). Hyphenated keys
# like ``api-key`` round-trip cleanly through a file but not through
# ``local.env``. The component only references the file path — no secret values
# pass through the renderer.
SECRET_STORE_FILE_TEMPLATE = """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: {name}
spec:
  type: secretstores.local.file
  version: v1
  metadata:
    - name: secretsFile
      value: {secrets_file}
    - name: nestedSeparator
      value: ":"
"""

FILE_SECRET_STORE_NAMES = ("secretstore", "deployment-secret-store")


def render_components(
    *,
    objectstore_root: str,
    eventstore_root: str,
    secrets_file: str | None = None,
) -> dict[str, str]:
    """Return ``{filename: yaml}`` for the default component set.

    Paths are inserted verbatim: resolving them, or creating the binding roots,
    is the caller's job (a container path must not be resolved on the host).
    When *secrets_file* is given, the ``secretstore`` and
    ``deployment-secret-store`` components are ``secretstores.local.file``
    stores reading that file instead of ``secretstores.local.env``.
    """
    rendered = {
        filename: template.format(
            objectstore_root=objectstore_root, eventstore_root=eventstore_root
        )
        for filename, template in COMPONENT_TEMPLATES.items()
    }
    if secrets_file is not None:
        for name in FILE_SECRET_STORE_NAMES:
            rendered[f"{name}.yaml"] = SECRET_STORE_FILE_TEMPLATE.format(
                name=name, secrets_file=secrets_file
            )
    return rendered
