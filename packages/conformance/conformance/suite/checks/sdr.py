"""P-series SDR-readiness checks (P029–P030, DISTR-752).

Cross-artifact checks that gate on ``self_deployed_runtime: true`` in
``atlan.yaml`` and verify two structural invariants:

* ``P029`` — every ``manifest.json`` under ``app/generated/`` must declare an
  ``agent_json`` slot in ``dag.extract.inputs.args``.  Missing it causes the
  SDR worker to start, the workflow to complete with status "success", and zero
  assets to land in the Atlan bucket — the silent-failure pattern from the
  MSSQL regression (atlan-mssql-app#177).

* ``P030`` — at least one Python source file (outside ``tests/``) must contain
  a ``self.upload(`` call so the ``ENABLE_ATLAN_UPLOAD`` path is reachable.
  Without it extraction "passes" but no assets transfer to the Atlan tenant
  bucket in SDR deployments.  Only applies to apps that actually have a
  publish stage: an app whose ``contract/app.pkl`` sets ``pipeline.publish =
  null`` compiles to a ``manifest.json`` with no ``dag.publish`` node, and has
  nowhere for ``self.upload()`` to hand extracted assets off to — P030 is
  skipped for those apps.

P026–P028 are reserved by a concurrent PR (GetattrOnTypedContractField,
AppStateAsCrossTaskChannel, ManualQualifiedNameFString — PR #2417).

Both are APP-scoped: the SDK itself never declares ``self_deployed_runtime``
and is always skipped.

``scan_path`` is a no-op — SDR checks are cross-artifact and require the full
path list plus the repo root.  The runner must call ``scan_all``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import discover, make_cli_main
from conformance.suite.schema.findings import Finding

SERIES = "P"
RULE_P029 = "P029"
RULE_P030 = "P030"

_SDR_FLAG_RE = re.compile(
    r"^self_deployed_runtime:\s*(true|false)\b",
    re.MULTILINE | re.IGNORECASE,
)

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def _is_sdr_app(root: Path) -> bool:
    """Return True when atlan.yaml declares self_deployed_runtime: true."""
    atlan_yaml = root / "atlan.yaml"
    if not atlan_yaml.is_file():
        return False
    try:
        text = atlan_yaml.read_text(encoding="utf-8")
    except OSError:
        return False
    m = _SDR_FLAG_RE.search(text)
    return m is not None and m.group(1).lower() == "true"


def _discover_manifests(root: Path) -> list[Path]:
    """All app/generated/manifest.json files: single-entrypoint or per-entrypoint."""
    generated = root / "app" / "generated"
    if not generated.is_dir():
        return []

    manifests: list[Path] = []
    root_manifest = generated / "manifest.json"
    if root_manifest.is_file():
        manifests.append(root_manifest)
    else:
        for child in sorted(generated.iterdir()):
            if child.is_dir():
                m = child / "manifest.json"
                if m.is_file():
                    manifests.append(m)
    return manifests


def _app_has_publish_stage(manifests: list[Path]) -> bool:
    """True unless every manifest structurally opts out of a publish stage.

    ``contract/app.pkl``'s ``pipeline.publish = null`` (an app-level opt-out of
    the publish pipeline stage) compiles to a manifest.json whose ``dag`` has
    no ``publish`` node. An app with no publish stage has nowhere for
    ``self.upload()`` to hand extracted assets off to, so P030 does not apply.

    No manifests at all (not yet generated) or an unparseable manifest means
    we cannot establish the opt-out, so this defaults to True (P030 still
    applies) rather than silently exempting an app we can't actually inspect.
    """
    if not manifests:
        return True
    for manifest_path in manifests:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        if (data.get("dag") or {}).get("publish") is not None:
            return True
    return False


def _check_p029(manifests: list[Path], root: Path) -> list[Finding]:
    """P029: every manifest.json under app/generated/ must have agent_json."""
    findings: list[Finding] = []
    for manifest_path in manifests:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        try:
            rel = manifest_path.relative_to(root)
        except ValueError:
            rel = manifest_path
        rel_str = str(rel)

        dag = data.get("dag", {})
        extract = dag.get("extract", {})
        inputs = extract.get("inputs", {})
        args = inputs.get("args", {})

        if not isinstance(args, dict) or "agent_json" not in args:
            findings.append(
                Finding(
                    rule_id=RULE_P029,
                    file=rel_str,
                    line=1,
                    column=1,
                    message=(
                        f"{rel_str}: manifest.json is missing 'agent_json' in "
                        "dag.extract.inputs.args. In SDR mode the platform fills "
                        "this slot with the credential-routing spec at dispatch time; "
                        "without the slot the workflow completes with status 'success' "
                        "but the extraction agent receives no credentials and writes "
                        "zero assets. Add agent_json to the extract inputs in "
                        "contract/app.pkl and regenerate."
                    ),
                )
            )

    return findings


def _check_p030(paths: list[Path]) -> list[Finding]:
    """P030: at least one source file must contain self.upload(."""
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "self.upload(" in text:
            return []

    return [
        Finding(
            rule_id=RULE_P030,
            file="atlan.yaml",
            line=1,
            column=1,
            message=(
                "No self.upload() call found in any app source file. In SDR mode "
                "ENABLE_ATLAN_UPLOAD gates whether extracted assets are transferred "
                "to the Atlan tenant bucket — if the gate is structurally unreachable "
                "the workflow completes with status 'success' but no assets land in "
                "the bucket. Add await self.upload(...) to the entrypoint or run() method."
            ),
        )
    ]


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: P029/P030 require cross-artifact analysis; use scan_all."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Check P029 and P030 for the repo at root.

    Parameters
    ----------
    paths:
        Python source files to inspect (as returned by :func:`discover`).
        These are the files checked by P030 for a ``self.upload(`` call.
    root:
        Repo root — used to locate ``atlan.yaml`` and ``app/generated/``.
    """
    if not _is_sdr_app(root):
        return []

    manifests = _discover_manifests(root)

    findings: list[Finding] = []
    findings.extend(_check_p029(manifests, root))
    if _app_has_publish_stage(manifests):
        findings.extend(_check_p030(paths))
    return findings


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "P029/P030: SDR-readiness checks — manifest agent_json slot (P029) "
        "and upload call presence (P030)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())
