"""K013 ManifestNodeAppNameMisattributed — toolkit-owned DAG node filed under AE (CNCT-24).

Detects a node in a committed generated ``manifest.json`` whose
``inputs.workflow_type`` is a workflow the contract-toolkit owns
(``QueryIntelligenceWorkflow``, ``PublishWorkflow``, ``LineageWorkflow``,
``PopularityWorkflow``, ``NotificationWorkflow``) while its ``app_name`` is
still the raw ``DAGNode`` default ``"automation-engine"``. Automation Engine
does not run those workflows, so the pairing always misattributes the node's
telemetry to AE.

This is a **cross-artifact** check over the committed ``app/generated/`` and
``contract/generated/`` trees; per-Python-file scanning has no meaning here, so
the module defines only ``scan_all`` — mirroring K006
(``manifest_contract``).
"""

from __future__ import annotations

import sys

from conformance.suite.checks._ast_common import discover, make_cli_main

from ._check import scan_all

SERIES = "K"

__all__ = ["SERIES", "discover", "main", "scan_all"]


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "K013 ManifestNodeAppNameMisattributed: flag generated manifest.json DAG "
        "nodes running a toolkit-owned workflow under app_name "
        "'automation-engine' (CNCT-24)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())
