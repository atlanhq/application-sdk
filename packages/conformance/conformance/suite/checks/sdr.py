"""P-series SDR-readiness checks (P029–P030, DISTR-752).

Cross-artifact checks that gate on ``self_deployed_runtime: true`` in
``atlan.yaml`` and verify two structural invariants:

* ``P029`` — an agent extraction manifest under ``app/generated/`` must surface
  ``agent_json`` AND ``extraction_method`` at the TOP LEVEL of
  ``dag.extract.inputs.args`` (that is the shape the platform reads to derive the
  agent queue + credential-routing spec at dispatch). Nesting them only under
  ``args.metadata`` strands the agent extraction on the cloud queue
  (atlan-tableau-app / atlan-snowflake-app); omitting them entirely is the
  MSSQL silent-zero-asset regression (atlan-mssql-app#177). Non-agent
  entrypoints (miner/QI, ``clean`` — no ``{{agent-json}}`` placeholder) are exempt.

* ``P030`` — at least one Python source file (outside ``tests/``) must contain
  a ``self.upload(`` call so the ``ENABLE_ATLAN_UPLOAD`` path is reachable.
  Without it extraction "passes" but no assets transfer to the Atlan tenant
  bucket in SDR deployments.  Only applies to apps that actually have a
  publish stage: an app whose ``contract/app.pkl`` sets
  ``pipeline.publish = null`` compiles to a ``manifest.json`` with no
  ``dag.publish`` node, and has nowhere for ``self.upload()`` to hand
  extracted assets off to — P030 is skipped for those apps.

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


#: Placeholder the Argo/AE template substitutes with the credential-routing spec
#: at dispatch. Its presence anywhere in an extract node's args marks that node as
#: an *agent* extraction (subject to the routing contract) vs. a miner/QI or
#: "clean" entrypoint that never resolves source credentials.
_AGENT_JSON_PLACEHOLDER = "{{agent-json}}"

#: Fields the platform reads at the extract-args TOP LEVEL to derive the agent
#: task queue (atlan-<agent-name>) and fill the credential-routing spec. Nesting
#: them only under args.metadata means the platform can't see them.
_REQUIRED_TOP_LEVEL_AGENT_FIELDS = ("agent_json", "extraction_method")


def _carries_agent_routing(obj: object) -> bool:
    """Whether the ``{{agent-json}}`` routing placeholder appears anywhere in
    ``obj`` (any depth) — the signal that an extract node is an agent extraction.
    """
    if isinstance(obj, dict):
        return any(_carries_agent_routing(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_carries_agent_routing(x) for x in obj)
    return obj == _AGENT_JSON_PLACEHOLDER


def _check_p029(manifests: list[Path], root: Path) -> list[Finding]:
    """P029: an SDR app's agent extraction manifests must surface ``agent_json``
    AND ``extraction_method`` at the TOP LEVEL of ``dag.extract.inputs.args``.

    Two failure modes are caught:

    * *misplaced / partial* — an agent-capable manifest (its extract args carry
      the ``{{agent-json}}`` placeholder) that nests the fields only under
      ``args.metadata`` or omits ``extraction_method``. The platform can't derive
      the agent queue or the credential-routing spec from them, so the agent
      extraction strands on the cloud queue / runs with no credentials
      (atlan-tableau-app, atlan-snowflake-app).
    * *missing entirely* — an SDR app whose generated manifests declare NO agent
      routing at all (the atlan-mssql-app#177 silent-zero-asset regression).

    A non-agent entrypoint (miner/QI, ``clean``) whose extract args carry no
    ``{{agent-json}}`` placeholder is exempt — it never resolves source
    credentials, so the contract does not apply to it.
    """
    findings: list[Finding] = []
    agent_capable_seen = False
    parsed_any = False
    for manifest_path in manifests:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        parsed_any = True

        try:
            rel = manifest_path.relative_to(root)
        except ValueError:
            rel = manifest_path
        rel_str = str(rel)

        dag = data.get("dag", {})
        extract = dag.get("extract", {})
        inputs = extract.get("inputs", {})
        args = inputs.get("args", {})
        if not isinstance(args, dict):
            args = {}

        # Exempt non-agent entrypoints (miner/QI, clean) — no routing placeholder.
        if not _carries_agent_routing(args):
            continue
        agent_capable_seen = True

        missing = [f for f in _REQUIRED_TOP_LEVEL_AGENT_FIELDS if f not in args]
        if missing:
            findings.append(
                Finding(
                    rule_id=RULE_P029,
                    file=rel_str,
                    line=1,
                    column=1,
                    message=(
                        f"{rel_str}: manifest.json must surface {missing} at the TOP "
                        "LEVEL of dag.extract.inputs.args. The platform (Heracles/AE) "
                        "derives the agent task queue and fills the credential-routing "
                        "spec from these top-level fields at dispatch; nesting them "
                        "only under args.metadata (or omitting extraction_method) "
                        "strands the agent extraction on the cloud queue / leaves it "
                        "with no credentials — the workflow reports 'success' but "
                        "writes zero assets. Surface them at the args top level in "
                        "contract/app.pkl (keep under metadata too if the connector "
                        "reads there) and regenerate."
                    ),
                )
            )

    # App-level: an SDR app with at least one parseable manifest but NO
    # agent-capable extraction node declares no agent routing anywhere — the
    # missing-slot regression (#177). Gated on parsed_any so an unparseable
    # manifest (which we skip) doesn't masquerade as "no agent routing".
    if parsed_any and not agent_capable_seen:
        findings.append(
            Finding(
                rule_id=RULE_P029,
                file="app/generated/",
                line=1,
                column=1,
                message=(
                    "No generated manifest.json under app/generated/ declares agent "
                    "routing (the {{agent-json}} slot in dag.extract.inputs.args). "
                    "In SDR mode the extraction agent then receives no credentials "
                    "and writes zero assets — a silent failure invisible to "
                    "status-only pipelines (atlan-mssql-app#177). Add agent_json + "
                    "extraction_method to the extract inputs in contract/app.pkl and "
                    "regenerate."
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
