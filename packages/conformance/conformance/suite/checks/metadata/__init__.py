"""M-series — customer-facing metadata conformance (BLDX-1575).

The first *model-driven* check series.  Apps declare customer-visible metadata in
``atlan.yaml`` (generated from ``contract/app.pkl``): app/entrypoint names and the
short/long descriptions that render in the marketplace and in customer tenants.
Two classes of problem there are judgement calls, not patterns:

* ``M001`` AppNamingConvention — a name embeds a vendor/company prefix against the
  fleet convention (e.g. "Amazon Redshift Miner").
* ``M002`` InternalJargonInDescription — a description carries internal jargon,
  codenames, or non-customer-appropriate language.

Both delegate the judgement to a pinned language model (see ``_model_common``).
This is a **cross-artifact** check keyed on ``atlan.yaml``, so ``scan_path`` is a
no-op and ``scan_all`` does the work.  When no model client is available (no API
key / ``anthropic`` not installed) the series **skips cleanly** and emits nothing,
so a keyless ``detect`` run is never turned red by this series.

The results are ordinary :class:`Finding`\\ s and flow through the identical
SARIF → Security-tab / connector-pulse / remediation pipeline as every
deterministic series; the ``atlan/mechanism="model"`` label plus per-finding
provenance (``atlan/modelId``, ``atlan/promptVersion``, ``atlan/evidence``) are
the only things that mark them non-deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

from conformance.suite.checks._ast_common import make_cli_main
from conformance.suite.checks._model_common import get_client, parse_yaml_directives
from conformance.suite.schema.findings import Finding

from . import _m001_naming, _m002_jargon
from ._surfaces import iter_surfaces

SERIES = "M"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def discover(root: Path) -> list[Path]:
    """Return the app's ``atlan.yaml`` if present (the only surface M reads)."""
    atlan_yaml = root / "atlan.yaml"
    return [atlan_yaml] if atlan_yaml.is_file() else []


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: the M-series is cross-artifact; use :func:`scan_all`."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Classify each discovered ``atlan.yaml`` for M001/M002 violations.

    Resolves the model client once; when none is available every path is skipped
    and an empty list is returned (clean offline behaviour).
    """
    client = get_client()
    if client is None:
        return []

    findings: list[Finding] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        surfaces = iter_surfaces(text)
        if not surfaces:
            continue
        directives = parse_yaml_directives(text)
        findings.extend(_m001_naming.scan_surfaces(surfaces, rel, directives, client))
        findings.extend(_m002_jargon.scan_surfaces(surfaces, rel, directives, client))
    return findings


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    default_scan_paths=(".",),
    description=(
        "M-series customer-facing metadata checks (model-driven): M001 app "
        "naming convention, M002 internal jargon in descriptions (BLDX-1575)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())
