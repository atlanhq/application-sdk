"""K031 PinnedWorkerTaskQueue — a hard-coded ``ATLAN_TASK_QUEUE`` in the deploy env.

The SDK derives the worker's Temporal task queue as
``atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}``
(``application_sdk.common.task_queue``). The chart suffixes the deployment name
with ``-od`` for the on-demand variant, so each variant polls its own queue. An
explicit ``ATLAN_TASK_QUEUE`` overrides the derivation: the ``-od`` worker then
polls the base queue, and the retry the activity rerouter moves to the ``-od``
queue is never picked up.

Reported once per app, where the fix belongs, like K029/K030: on the
``["ATLAN_TASK_QUEUE"] =`` line in ``contract/**/*.pkl``, else on the key under
``deploy.env`` / ``pools.<name>.env`` in ``atlan.yaml``. An app with only
``atlan.yaml`` (no ``contract/``) is still checked.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
    safe_read_text,
    strip_yaml_comment,
)
from conformance.suite.checks.dapr_objectstore import (
    _YAML_KV_RE,
    contract_false_findings,
    contract_files,
)
from conformance.suite.schema.findings import Finding

SERIES = "K"
_RULE_ID = "K031"

__all__ = ["SERIES", "discover", "main", "scan_all"]

# Masked code keeps the brackets and ``=`` but blanks the string key; the raw
# text under the masked match must be the ATLAN_TASK_QUEUE key.
_PKL_MASKED_RE = re.compile(r"\[\s*\]\s*=")
_PKL_RAW_RE = re.compile(r'\[\s*"ATLAN_TASK_QUEUE"\s*\]\s*=')

_CAUSE = (
    "pins ATLAN_TASK_QUEUE in the deploy env, which overrides the SDK's "
    "per-deployment queue atlan-{app}-{deployment}. The on-demand (-od) worker "
    "then polls the base queue, and retries the activity rerouter moves to the "
    "-od queue are never picked up — the run hangs until its timeout."
)
_CAVEAT = (
    " If the pinned value is the queue the SDK derives anyway "
    "(atlan-<app name>-<deployment>), delete the line"
)
_LEGACY = (
    " If it pins a different, legacy queue name, deleting it moves the worker "
    "off the queue the manifest dispatches to: first move the manifest's task "
    "queue and the scaling trigger to the derived name, or suppress with a reason."
)
_CONTRACT_MSG = (
    f"The contract {_CAUSE}{_CAVEAT} and regenerate atlan.yaml with the repo's "
    f"generate task.{_LEGACY}"
)
_YAML_GENERATED_MSG = (
    f"atlan.yaml {_CAUSE}{_CAVEAT} from the contract's deploy env (or bump "
    "contract-toolkit) and regenerate; atlan.yaml is generated, do not edit it."
    f"{_LEGACY}"
)
_YAML_ONLY_MSG = f"atlan.yaml {_CAUSE}{_CAVEAT} from atlan.yaml.{_LEGACY}"


def discover(root: Path) -> list[Path]:
    """``[root]`` for an app with ``atlan.yaml`` or a ``contract/`` pkl; else ``[]``.

    Unlike K029/K030 this does not require ``contract/``: an app can ship only
    a hand-written ``atlan.yaml``.
    """
    return [root] if (root / "atlan.yaml").is_file() or contract_files(root) else []


def _pinned_lines(text: str) -> list[int]:
    """1-based lines of ``ATLAN_TASK_QUEUE`` under ``deploy.env`` or ``pools.*.env``.

    An indentation walk, as in K029's ``deploy_dapr_false_line``; any value counts.
    """
    path: list[tuple[int, str]] = []  # (indent, key) of the open mappings
    hits: list[int] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _YAML_KV_RE.match(raw)
        if m is None:
            continue
        indent = len(m.group("indent"))
        while path and path[-1][0] >= indent:
            path.pop()
        key = m.group("key")
        keys = [k for _, k in path]
        if key == "ATLAN_TASK_QUEUE" and (
            keys == ["deploy", "env"]
            or (len(keys) == 3 and keys[0] == "pools" and keys[2] == "env")
        ):
            hits.append(lineno)
        if not strip_yaml_comment(m.group("value")).strip():
            path.append((indent, key))
    return hits


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Emit K031 at the contract source if it pins the queue, else at atlan.yaml."""
    if not paths:
        return []
    has_contract = bool(contract_files(root))
    findings = contract_false_findings(
        root,
        rule_id=_RULE_ID,
        pattern=_PKL_MASKED_RE,
        raw_pattern=_PKL_RAW_RE,
        message=_CONTRACT_MSG,
    )
    if findings:
        return findings
    atlan = root / "atlan.yaml"
    if not atlan.is_file():
        return []
    text = safe_read_text(atlan) or ""
    suppressions = parse_toml_suppressions(text)
    return [
        make_toml_finding(
            rule_id=_RULE_ID,
            file="atlan.yaml",
            line=line,
            column=1,
            message=_YAML_GENERATED_MSG if has_contract else _YAML_ONLY_MSG,
            suppressions=suppressions,
        )
        for line in _pinned_lines(text)
    ]


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description="K031: a hard-coded ATLAN_TASK_QUEUE in the deploy env.",
)


if __name__ == "__main__":
    sys.exit(main())
