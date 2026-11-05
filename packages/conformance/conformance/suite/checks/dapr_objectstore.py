"""K029 DeploymentObjectStoreDisabled — the deployment object store switched off.

The SDK requires the deployment object store and fails at startup when the
atlan-app chart omits its Dapr component, which the chart does when
``atlan.yaml`` carries ``deploy.dapr.objectstore: false``.

The rule reports the setting once per app, where the fix belongs:

* on an explicit ``objectstore = false`` in ``contract/**/*.pkl`` when the
  contract says it;
* otherwise on the ``objectstore: false`` line under ``deploy:`` → ``dapr:`` in
  the generated ``atlan.yaml``. That is the inherited case: older
  contract-toolkit releases defaulted ``objectstore`` to ``false`` and rendered
  the whole ``dapr`` block as soon as any other component was enabled.

APP-scoped and gated on a ``contract/`` directory, like the sibling K checks.
It reads fixed artifacts across the repo, so it implements ``scan_all`` and a
no-op ``scan_path``, mirroring ``release_contract``.
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
)
from conformance.suite.checks.legacy_contract._directives_pkl import (
    _make_pkl_finding_suppressed,
    _parse_pkl_directives,
)
from conformance.suite.schema.findings import Finding

SERIES = "K"
_RULE_ID = "K029"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]

# ``objectstore = false`` anywhere on a pkl line, including the one-line form
# ``dapr { objectstore = false; secretstore = true }``. Matched against the
# line with its ``//`` comment stripped, so a commented-out assignment is not a
# setting.
_PKL_FALSE_RE = re.compile(r"\bobjectstore\s*=\s*false\b")

# YAML key/value on one line: indentation, key, value (comment not yet removed).
_YAML_KV_RE = re.compile(
    r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][\w-]*)[ \t]*:(?P<value>.*)$"
)

# YAML 1.1 false spellings; contract-toolkit renders ``false``.
_YAML_FALSE = frozenset({"false", "False", "FALSE", "no", "No", "NO", "off", "Off"})

_FIX = (
    "Set objectstore = true in the contract's deploy.dapr block (or drop the "
    "explicit false and take a contract-toolkit whose default is true), then "
    "regenerate atlan.yaml with the repo's generate task."
)


def discover(root: Path) -> list[Path]:
    """Return ``[root]`` for a pkl-contract-driven app repo, else ``[]``."""
    return [root] if (root / "contract").is_dir() else []


def _contract_files(root: Path) -> list[Path]:
    """Hand-written ``contract/**/*.pkl``, skipping hidden dirs (pkl caches)."""
    contract_dir = root / "contract"
    return sorted(
        p
        for p in contract_dir.rglob("*.pkl")
        if not any(part.startswith(".") for part in p.relative_to(contract_dir).parts)
    )


def _contract_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _contract_files(root):
        text = safe_read_text(path)
        if text is None:
            continue
        directives = _parse_pkl_directives(text)
        rel = path.relative_to(root).as_posix()
        for lineno, raw in enumerate(text.splitlines(), start=1):
            code = raw.split("//", 1)[0]
            if not _PKL_FALSE_RE.search(code):
                continue
            suppressed, justification = _make_pkl_finding_suppressed(
                rule_id=_RULE_ID, line=lineno, directives=directives
            )
            findings.append(
                Finding(
                    rule_id=_RULE_ID,
                    file=rel,
                    line=lineno,
                    column=1,
                    message=(
                        "The contract sets deploy.dapr.objectstore to false. The "
                        "atlan-app chart then omits the object-store component "
                        "the SDK requires, and the app's worker and handler fail "
                        f"at startup. {_FIX}"
                    ),
                    suppressed=suppressed,
                    suppression_justification=justification,
                )
            )
    return findings


def _deploy_dapr_objectstore_false_line(text: str) -> int | None:
    """1-based line of ``objectstore: false`` under top-level ``deploy:`` → ``dapr:``.

    An indentation walk rather than a YAML parse: the package carries no YAML
    dependency, and the line number is what the finding anchors on. Only the
    block path ``deploy.dapr.objectstore`` counts — the chart reads nothing else.
    """
    path: list[tuple[int, str]] = []  # (indent, key) of the open mappings
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
        value = m.group("value").split(" #", 1)[0].strip()
        keys = [k for _, k in path]
        if keys == ["deploy", "dapr"] and key == "objectstore":
            return lineno if value in _YAML_FALSE else None
        if not value:
            path.append((indent, key))
    return None


def _atlan_yaml_findings(root: Path) -> list[Finding]:
    atlan = root / "atlan.yaml"
    if not atlan.is_file():
        return []
    text = safe_read_text(atlan) or ""
    line = _deploy_dapr_objectstore_false_line(text)
    if line is None:
        return []
    return [
        make_toml_finding(
            rule_id=_RULE_ID,
            file="atlan.yaml",
            line=line,
            column=1,
            message=(
                "atlan.yaml carries deploy.dapr.objectstore: false, so the "
                "atlan-app chart omits the object-store component the SDK "
                "requires and the app's worker and handler fail at startup. "
                "atlan.yaml is generated — do not edit it. " + _FIX
            ),
            # atlan.yaml uses '#' comments, so the shared directive scanner applies.
            suppressions=parse_toml_suppressions(text),
        )
    ]


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Emit K029 once per app: at the contract source if it says so, else atlan.yaml."""
    if not paths:
        return []
    return _contract_findings(root) or _atlan_yaml_findings(root)


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: K029 is cross-artifact; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description="K029: the deployment object store is disabled in deploy.dapr.",
)


if __name__ == "__main__":
    sys.exit(main())
