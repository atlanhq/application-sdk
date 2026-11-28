"""K001/K002 legacy-contract checks — contract-toolkit conformance (BLDX-1479, FND-5546).

Detects contract ``.pkl`` files that are still built on one of the five legacy
toolkit modules (K001) or that carry NativeApp-only APIs and legacy imports
that ``App.pkl`` dropped (K002).

This is a per-file scan (``scan_path``).  Unlike P016/P025, which read the
committed ``pkl eval`` output artifacts, this check must read the ``.pkl``
*source* because the App.pkl-vs-legacy distinction lives entirely in the
``amends`` / ``extends`` / ``import`` lines and NativeApp-only property names —
information that ``pkl eval`` erases from its generated output.

Files scanned (``discover``): ``contract/**/*.pkl`` plus every ``.pkl`` file in
any directory whose ``PklProject`` depends on the toolkit.

* ``K001`` ContractAmendsLegacyModule — ``amends`` / ``extends`` of
  ``NativeApp.pkl``, ``NativeAppBundle.pkl``, ``Config.pkl``,
  ``Credential.pkl`` or ``Renderers.pkl`` (any spelling, including the
  ``package://`` URI form), or an object built as ``(X) { … }`` /
  ``new X { … }`` where ``X`` is a legacy-module import.

* ``K002`` LegacyContractApi — NativeApp-only properties
  (``flatManifestArgs``, ``manifestMetadataArgs``, ``workflowTypeOverride``)
  or an ``import`` of any of the five legacy modules.  ``Connectors.pkl`` is a
  current module and is not flagged.
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
        "K001/K002 legacy-contract conformance: flag contract .pkl files that "
        "are still built on a legacy toolkit module (NativeApp, NativeAppBundle, "
        "Config, Credential, Renderers) or carry NativeApp-only APIs "
        "(flatManifestArgs, workflowTypeOverride, legacy imports). "
        "Migrate to App.pkl per contract-toolkit v0.10.0+ (BLDX-1479)."
    ),
    default_tool_version=TOOL_VERSION,
)

if __name__ == "__main__":
    sys.exit(main())
