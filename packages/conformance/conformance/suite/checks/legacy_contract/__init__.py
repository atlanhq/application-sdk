"""K001/K002 legacy-contract checks — contract-toolkit conformance (BLDX-1479).

Detects ``contract/**/*.pkl`` files that still amend the legacy
``NativeApp.pkl`` / ``NativeAppBundle.pkl`` base modules (K001) or that carry
NativeApp-only APIs that ``App.pkl`` dropped (K002).

This is a per-file scan (``scan_path``).  Unlike P016/P025, which read the
committed ``pkl eval`` output artifacts, this check must read the ``.pkl``
*source* because the App.pkl-vs-legacy distinction lives entirely in the
``amends`` line and NativeApp-only property names — information that
``pkl eval`` erases from its generated output.

Currently implemented:

* ``K001`` ContractAmendsLegacyModule — the ``amends`` line points at
  ``NativeApp.pkl`` or ``NativeAppBundle.pkl`` instead of ``App.pkl``.

* ``K002`` LegacyContractApi — the file contains NativeApp-only properties
  (``flatManifestArgs``, ``manifestMetadataArgs``, ``workflowTypeOverride``)
  or legacy imports (``Config.pkl``, ``Connectors.pkl``, ``Credential.pkl``,
  ``Renderers.pkl``).
"""

from __future__ import annotations

import sys

from conformance.suite.checks._ast_common import (
    TOOL_VERSION,
    make_cli_main,
    make_scan_path,
)
from conformance.suite.schema.findings import Finding

from ._scan import discover, scan_text

SERIES = "K"

__all__ = ["SERIES", "discover", "main", "scan_path", "Finding"]


scan_path = make_scan_path(scan_text)


main = make_cli_main(
    scan_text=scan_text,
    discover=discover,
    description=(
        "K001/K002 legacy-contract conformance: flag contract/**/*.pkl files that "
        "still amend NativeApp.pkl/NativeAppBundle.pkl or carry NativeApp-only APIs "
        "(flatManifestArgs, workflowTypeOverride, legacy imports). "
        "Migrate to App.pkl per contract-toolkit v0.10.0+ (BLDX-1479)."
    ),
    default_tool_version=TOOL_VERSION,
)

if __name__ == "__main__":
    sys.exit(main())
