"""Manifest-vs-code cross-checks over the generated ``app/generated/`` tree.

* ``K006`` ManifestContractFieldMismatch (BLDX-1527) — a
  ``$.<node>.outputs.<field>`` JSONPath reference in the generated
  ``app/generated/**/manifest.json`` DAG that the referenced entrypoint's Python
  ``Output`` contract does not declare, directly or via an inherited base/mixin
  (e.g. ``application_sdk.contracts.base.PublishInputMixin``).
* ``K015`` LegacyWorkflowTypeContractDrift (CONNECT-1081) — the manifest's
  ``legacy_workflow_types`` block and the SDK's ``App.legacy_workflow_types``
  class attribute must declare the same aliases and the same expiry.
* ``K018`` ManifestArgNotDeclaredOnInputContract — the Input-side
  mirror of K006: an ``extract``-node arg the entrypoint's ``Input`` contract
  cannot receive is dropped by Pydantic, and the run silently falls back to the
  field's default.
* ``K021`` FilterFieldRejectsAeString (CONNECT-1333 / CONNECT-1389) — the
  type-aware sibling of K018: an ``include_*`` / ``exclude_*`` field the
  entrypoint's ``Input`` contract types as a strict ``dict`` with no ``str``
  union, no coercing ``ExtractionInput`` base, and no ``mode="before"``
  validator rejects the flat JSON *string* the AE sends and crashes the run.
* ``K019`` FormKeyMissingFromManifestArgs (WARE-1323) — a ``uiConfig`` form key
  with no ``{{...}}`` placeholder in any manifest never reaches the run *and*
  never persists, because the args template doubles as the persistence schema.
* ``K020`` ManifestArgsLegacyNestedEnvelope — the ``extract`` node still emits
  the legacy ``args.metadata{}`` envelope instead of flat top-level args, either
  because the contract opts out via ``flatManifestArgs = false`` or because the
  committed manifest is a stale pre-flattening artifact.

Both are **cross-file + cross-artifact** checks: they scan all Python files, then
read the committed ``app/generated/`` tree. Per-file scanning has no meaning
here, so ``scan_path`` is a no-op and ``scan_all`` does all the work — mirrors
P016 (``entrypoint_alignment``), the closest existing cross-artifact check.
"""

from __future__ import annotations

import sys
from pathlib import Path

from conformance.suite.checks._ast_common import discover, make_cli_main
from conformance.suite.schema.findings import Finding

from ._check import scan_all as _scan_field_mismatch
from ._filter_string_acceptance import scan_all as _scan_filter_string_acceptance
from ._form_keys import scan_all as _scan_form_keys
from ._input_fields import scan_all as _scan_input_fields
from ._legacy_aliases import scan_all as _scan_legacy_aliases
from ._nested_envelope import scan_all as _scan_nested_envelope

SERIES = "K"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: these rules need cross-file + cross-artifact analysis; use :func:`scan_all`."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Run every manifest-vs-code cross-check in this package.

    Each rule gates itself independently: K006 no-ops on a repo whose entrypoint
    ``Output`` contracts it cannot resolve, which says nothing about whether
    K015 can compare the alias declarations.
    """
    return [
        *_scan_field_mismatch(paths, root),
        *_scan_legacy_aliases(paths, root),
        *_scan_input_fields(paths, root),
        *_scan_filter_string_acceptance(paths, root),
        *_scan_form_keys(paths, root),
        *_scan_nested_envelope(paths, root),
    ]


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "Manifest-vs-code cross-checks: K006 verifies manifest.json "
        "$.<node>.outputs.<field> refs against the Python Output contract "
        "(BLDX-1527); K015 verifies the legacy_workflow_types block against the "
        "SDK App declaration (CONNECT-1081); K018 verifies the extract node's "
        "args against the Python Input contract; K019 verifies "
        "uiConfig form keys are wired into the args template (WARE-1323); "
        "K020 flags a manifest still nesting args under metadata; "
        "K021 flags a filter field that rejects the AE's flat JSON string "
        "(CONNECT-1333 / CONNECT-1389)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())
