"""P-series credential-seam check — AST-based (FND-2949).

Enforces that a workflow input's credential channels are routed by the SDK
rather than by the app:

* ``P053`` LocalCredentialRouting (app) — app code calling
  ``CredentialRef.resolve`` / ``resolve_or_none`` or building
  ``CredentialRef(credential_guid=...)`` itself, or declaring its own
  ``CredentialValue`` / ``Bounded*Credential*`` type alias, instead of using
  ``application_sdk.credentials.route_credentials`` and the types it exports.

Registered under series letter ``P`` alongside ``prescriptions``,
``client_seam``, ``persistence_seam`` and the other seam checks, the established
multi-module pattern.

SDK-version gate
----------------
The prescribed API first ships in application-sdk 3.40.0.  :func:`scan_all` and
:func:`scan_path` read the SDK version the repo's ``uv.lock`` resolves (the same
reader ``P051`` uses) and return nothing unless it is confirmed at or above that
floor — no lock, no SDK in it, or an unparseable version all stay silent.
:func:`scan_text` is the pure AST pass with no repo to read, so it is ungated.

Scope note
----------
``P053`` is ``app``-scoped and the runner filters out-of-scope findings before
they reach the report, so the SDK's own ``routing.py`` — which calls
``CredentialRef.resolve`` and defines ``CredentialValue`` by definition — needs
no self-exemption guard here.

Inline suppression
------------------
Add ``# conformance: ignore[P053] <reason>`` on the offending line (or the
comment-only line directly above it).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    discover,
    make_cli_main,
)
from conformance.suite.checks._version import (
    locked_sdk_version,
    parse_version,
    version_reached,
)
from conformance.suite.schema.findings import Finding

from ._local_credential_routing import check_p053

SERIES = "P"

#: First application-sdk release that exports ``route_credentials`` and its
#: types.  The check stays silent for an app whose ``uv.lock`` resolves below it.
ROUTE_CREDENTIALS_SDK_FLOOR = (3, 40, 0)

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path", "scan_text"]


def sdk_has_credential_seam(root: Path) -> bool:
    """True iff ``root/uv.lock`` resolves an SDK that exports ``route_credentials``.

    Fails closed — an unreadable version is not evidence the seam exists, and
    prescribing an import the app cannot make is a finding nobody can act on.
    """
    locked = locked_sdk_version(root)
    if locked is None:
        return False
    parsed = parse_version(locked)
    return parsed is not None and version_reached(ROUTE_CREDENTIALS_SDK_FLOOR, parsed)


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for P053 (ungated — no repo to read)."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    return check_p053(tree, file, _parse_directives(text))


def _scan_file(path: Path, root: Path) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file, gated on *root*'s locked SDK version."""
    if not sdk_has_credential_seam(root):
        return []
    return _scan_file(path, root)


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Scan every discovered file, reading *root*'s ``uv.lock`` once for the gate."""
    if not sdk_has_credential_seam(root):
        return []
    return [finding for path in paths for finding in _scan_file(path, root)]


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "Credential-seam P-series check (P053): scan Python files for app-side "
        "credential routing and local credential-type aliases where the SDK's "
        "route_credentials applies (silent below application-sdk 3.40.0)."
    ),
)
"""CLI entry point for the credential-seam check."""


if __name__ == "__main__":
    sys.exit(main())
