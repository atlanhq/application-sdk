"""K030 CredentialSecretStoreDisabled — a credential app with the secret store off.

The SDK resolves every credential through the Dapr secret store. When
``atlan.yaml`` carries ``deploy.dapr.secretstore: false`` the deployer leaves the
component out, and every credential lookup fails with HTTP 500 from
``/v1.0/secrets/<app>-secretstore/<guid>``.

The rule fires only for an app that resolves credentials, judged from the
contract source with the predicate contract-toolkit uses to default
``secretstore``: some file amending ``App.pkl`` keeps ``hasCredentialConfig`` on
(it defaults to on unless ``type = "system"``) or declares
``additionalCredentialConfigs``.

It reports the setting once per app, where the fix belongs, like K029:

* on an explicit ``secretstore = false`` in ``contract/**/*.pkl``;
* otherwise on the ``secretstore: false`` line under ``deploy:`` → ``dapr:`` in
  the generated ``atlan.yaml``. That is the inherited case: contract-toolkit
  releases between the object-store default change and this rule rendered
  ``secretstore: false`` for every app with a ``deploy`` block.

APP-scoped and gated on a ``contract/`` directory, like K029.
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
from conformance.suite.checks.dapr_objectstore import (
    contract_files,
    deploy_dapr_false_line,
    discover,
)
from conformance.suite.checks.legacy_contract._directives_pkl import (
    _make_pkl_finding_suppressed,
    _parse_pkl_directives,
)
from conformance.suite.schema.findings import Finding

SERIES = "K"
_RULE_ID = "K030"

__all__ = [
    "SERIES",
    "app_uses_credentials",
    "discover",
    "main",
    "scan_all",
    "scan_path",
]

# ``secretstore = false`` anywhere on a pkl line, including the one-line form
# ``dapr { objectstore = true; secretstore = false }``. Matched against the line
# with its ``//`` comment stripped.
_PKL_FALSE_RE = re.compile(r"\bsecretstore\s*=\s*false\b")

# A file is an App contract when it amends or extends contract-toolkit's App.pkl
# (``@app-contract-toolkit/App.pkl`` or a relative path to it). Helper modules
# such as a shared ``CredentialConfig`` are not contracts and carry no defaults.
_APP_CONTRACT_RE = re.compile(r'^\s*(?:amends|extends)\s+"[^"]*\bApp\.pkl"', re.M)
_HAS_CREDS_TRUE_RE = re.compile(r"^\s*hasCredentialConfig\s*=\s*true\b", re.M)
_HAS_CREDS_FALSE_RE = re.compile(r"^\s*hasCredentialConfig\s*=\s*false\b", re.M)
_SYSTEM_TYPE_RE = re.compile(r'^\s*type\s*=\s*"system"', re.M)
_ADDITIONAL_CREDS_RE = re.compile(r"^\s*additionalCredentialConfigs\b", re.M)

_FIX = (
    "Remove the explicit secretstore = false from the contract's deploy.dapr "
    "block (contract-toolkit then renders true for an app with credentials) or "
    "set secretstore = true, then regenerate atlan.yaml with the repo's generate "
    "task."
)


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def _contract_uses_credentials(code: str) -> bool:
    """Mirror contract-toolkit's ``hasCredentialConfig || additionalCredentialConfigs``."""
    if _ADDITIONAL_CREDS_RE.search(code) or _HAS_CREDS_TRUE_RE.search(code):
        return True
    if _HAS_CREDS_FALSE_RE.search(code):
        return False
    # ``hasCredentialConfig = !isSystemApp``.
    return not _SYSTEM_TYPE_RE.search(code)


def app_uses_credentials(root: Path) -> bool:
    """True when any App contract under ``contract/`` resolves credentials.

    A bundle root may set ``hasCredentialConfig = false`` while its entrypoint
    contracts keep it on; contract-toolkit counts those too, and so does this.
    """
    for path in contract_files(root):
        text = safe_read_text(path)
        if text is None:
            continue
        code = _strip_comments(text)
        if _APP_CONTRACT_RE.search(code) and _contract_uses_credentials(code):
            return True
    return False


def _contract_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in contract_files(root):
        text = safe_read_text(path)
        if text is None:
            continue
        directives = _parse_pkl_directives(text)
        rel = path.relative_to(root).as_posix()
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if not _PKL_FALSE_RE.search(raw.split("//", 1)[0]):
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
                        "The contract sets deploy.dapr.secretstore to false, but "
                        "the app resolves credentials. The deployer then leaves out "
                        "the secret store the SDK reads every credential through, "
                        f"and each credential lookup fails with HTTP 500. {_FIX}"
                    ),
                    suppressed=suppressed,
                    suppression_justification=justification,
                )
            )
    return findings


def _atlan_yaml_findings(root: Path) -> list[Finding]:
    atlan = root / "atlan.yaml"
    if not atlan.is_file():
        return []
    text = safe_read_text(atlan) or ""
    line = deploy_dapr_false_line(text, "secretstore")
    if line is None:
        return []
    return [
        make_toml_finding(
            rule_id=_RULE_ID,
            file="atlan.yaml",
            line=line,
            column=1,
            message=(
                "atlan.yaml carries deploy.dapr.secretstore: false, but the app "
                "resolves credentials. The deployer leaves out the secret store "
                "the SDK reads every credential through, and each credential "
                "lookup fails with HTTP 500. atlan.yaml is generated — do not "
                "edit it. Bump contract-toolkit (which renders true for an app "
                "with credentials) or set secretstore = true in the contract, "
                "then regenerate."
            ),
            # atlan.yaml uses '#' comments, so the shared directive scanner applies.
            suppressions=parse_toml_suppressions(text),
        )
    ]


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Emit K030 once per credential app: at the contract source, else atlan.yaml."""
    if not paths or not app_uses_credentials(root):
        return []
    return _contract_findings(root) or _atlan_yaml_findings(root)


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: K030 is cross-artifact; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description="K030: a credential app disables the Dapr secret store in deploy.dapr.",
)


if __name__ == "__main__":
    sys.exit(main())
