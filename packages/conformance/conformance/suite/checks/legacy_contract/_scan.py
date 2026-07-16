"""K-series regex scanner — detect legacy contract-toolkit base modules and APIs.

Design notes
------------
This scanner operates on ``.pkl`` *source* (the ``contract/**/*.pkl`` files
authored by app developers), **not** on the ``pkl eval`` generated artifacts
(``atlan.yaml``, ``app/generated/**``).  The distinction from P016/P025 is
deliberate: the App.pkl-vs-legacy difference lives entirely in the ``amends``
line and NativeApp-only property names, all of which ``pkl eval`` erases.

No ``pkl`` CLI is invoked here — just stdlib ``re``.  The scanner is therefore
safe to run in any environment, including CI runners that have no ``pkl``
installed.

Suppression
-----------
Pkl uses ``//`` for line comments.  The Python ``# conformance: ignore``
infrastructure cannot be reused; the pkl-specific parser lives in
``_directives_pkl.py``.

Block-comment stripping
-----------------------
``/* … */`` blocks are stripped before scanning so that commented-out ``amends``
lines and properties are not reported.  The stripping is simple (no nested blocks,
no string awareness) but adequate for the contract files the toolkit produces.
"""

from __future__ import annotations

import re
from pathlib import Path

from conformance.suite.schema.findings import Finding

from ._directives_pkl import _make_pkl_finding_suppressed, _parse_pkl_directives

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# K001 — amends a legacy base module instead of App.pkl.
# Matches: amends "…NativeApp.pkl" or amends "…NativeAppBundle.pkl"
# The package form: amends "@app-contract-toolkit/NativeApp.pkl"
# The relative form: amends "../../src/NativeApp.pkl"
# The (?:[^"]*/)? prefix requires the module name to sit immediately after a
# path separator (or at the start of the string), so "MyNativeApp.pkl" and
# "NotNativeApp.pkl" are not matched — only the exact legacy names.
_K001_RE = re.compile(
    r'\bamends\s+"(?:[^"]*/)?(?:NativeApp|NativeAppBundle)\.pkl"',
)

# K002 — NativeApp-only property names and legacy imports.
# Split into two sub-patterns for precise column anchoring in the message.
_K002_PROP_RE = re.compile(
    r"\b(flatManifestArgs|manifestMetadataArgs|workflowTypeOverride)\b"
)
# Same (?:[^"]*/)? anchor as K001: "AppConfig.pkl" and "MyConfig.pkl" must not
# match — only the exact legacy module names directly after a path separator.
#
# Connectors.pkl is deliberately NOT here: unlike Config/Credential/Renderers
# (Argo-era, dropped by App.pkl), the Connectors registry is still imported
# explicitly by consumers — App.pkl imports it internally and types `connector`
# as `Connectors.Type`, but does not re-export the constants, so every current
# toolkit example still `import`s it. Flagging it is a false positive.
_K002_IMPORT_RE = re.compile(
    r'\bimport\s+"(?:[^"]*/)?(?:Config|Credential|Renderers)\.pkl"'
)

# K002b exemption — a contract that *amends* Credential.pkl is a credential-config
# sub-contract, not an App.pkl entrypoint, and legitimately imports Config.pkl for
# its widget types.  Credential.pkl uses ``Config.*`` internally and, unlike
# App.pkl, does NOT re-export those widgets as unqualified typealiases, so a
# credential contract that referenced them without the import would fail
# ``pkl eval`` ("Cannot find type TextInput").  K002b's premise — "these imports
# are not needed because App.pkl re-exports them" — simply does not hold for a
# Credential.pkl base, so the legacy-import check is skipped for such files.
# (App.pkl- and NativeApp.pkl-amending contracts are unaffected: there the import
# really is redundant / legacy and still fires.)  Same ``(?:[^"]*/)?`` anchoring
# as K001 so only the exact ``Credential.pkl`` base matches.
_K002_CREDENTIAL_BASE_RE = re.compile(
    r'\bamends\s+"(?:[^"]*/)?Credential\.pkl"'
)

# ---------------------------------------------------------------------------
# Block-comment stripping
# ---------------------------------------------------------------------------

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_block_comments(text: str) -> str:
    """Replace ``/* … */`` spans with an equal number of newlines.

    Preserving line count ensures that the 1-based line numbers reported in
    findings remain accurate even after stripping.
    """

    def _replace(m: re.Match[str]) -> str:
        return "\n" * m.group(0).count("\n")

    return _BLOCK_COMMENT_RE.sub(_replace, text)


# ---------------------------------------------------------------------------
# Per-line scanning
# ---------------------------------------------------------------------------

_LINE_COMMENT_RE = re.compile(r"//.*$")


def _strip_line_comment(line: str) -> str:
    """Strip the trailing ``// …`` portion from a line.

    Applied to every non-blank line before K001, K002a, and K002b pattern
    matching so that inline trailing comments are ignored symmetrically across
    all three sub-patterns.  For example, a line such as::

        name = "foo"  // amends "@x/NativeApp.pkl"

    must not trigger K001 — the amends target is in a comment, not in code.
    """
    return _LINE_COMMENT_RE.sub("", line)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def scan_text(text: str, rel: str) -> list[Finding]:
    """Scan the pkl source *text* and return K-series findings.

    Parameters
    ----------
    text:
        Raw content of a ``contract/**/*.pkl`` file.
    rel:
        Repo-root-relative path (used as the ``file`` field in findings).

    Returns
    -------
    list[Finding]
        One entry per violation, with ``suppressed=True`` when a
        ``// conformance: ignore[...]`` directive covers the line.
    """
    # Parse suppression directives from the original (un-stripped) source so
    # that directives inside block comments are not honoured (conservative).
    directives = _parse_pkl_directives(text)

    # Strip block comments before scanning so commented-out code is ignored.
    clean = _strip_block_comments(text)
    lines = clean.splitlines()

    # A credential-config sub-contract (``amends Credential.pkl``) legitimately
    # imports Config.pkl for its widget types, so the K002b legacy-import check
    # does not apply to it (see _K002_CREDENTIAL_BASE_RE).  Detect the base once,
    # honouring the same comment handling the main scan uses (skip ``//``-only
    # lines, strip trailing comments) so a commented-out amends line is ignored.
    base_is_credential = any(
        _K002_CREDENTIAL_BASE_RE.search(_strip_line_comment(ln))
        for ln in lines
        if not ln.strip().startswith("//")
    )

    findings: list[Finding] = []

    for lineno, line in enumerate(lines, start=1):
        # Skip pure line comments — nothing on a ``// …``-only line is a
        # violation, and we don't want the comment text itself to trigger K002.
        stripped = line.strip()
        if stripped.startswith("//"):
            continue

        # Strip the trailing ``// …`` portion once, before all three
        # sub-patterns, so that inline trailing comments are ignored
        # symmetrically: a property name or amends target that appears only
        # inside a comment is never flagged.
        code_only = _strip_line_comment(line)

        # ── K001: amends legacy module ────────────────────────────────────
        m = _K001_RE.search(code_only)
        if m:
            module = (
                "NativeAppBundle.pkl"
                if "NativeAppBundle" in m.group(0)
                else "NativeApp.pkl"
            )
            col = m.start() + 1
            suppressed, justification = _make_pkl_finding_suppressed(
                rule_id="K001", line=lineno, directives=directives
            )
            findings.append(
                Finding(
                    rule_id="K001",
                    file=rel,
                    line=lineno,
                    column=col,
                    message=(
                        f"Contract amends {module!r} — migrate to "
                        f'App.pkl (amends "@app-contract-toolkit/App.pkl").  '
                        f"See contract-toolkit/docs/reference.md and the "
                        f"make-contract skill for migration guidance.  "
                        f"Suppress with: // conformance: ignore[K001] <reason>"
                    ),
                    suppressed=suppressed,
                    suppression_justification=justification,
                )
            )

        # ── K002a: NativeApp-only property names ──────────────────────────
        for pm in _K002_PROP_RE.finditer(code_only):
            prop = pm.group(1)
            col = pm.start() + 1
            suppressed, justification = _make_pkl_finding_suppressed(
                rule_id="K002", line=lineno, directives=directives
            )
            findings.append(
                Finding(
                    rule_id="K002",
                    file=rel,
                    line=lineno,
                    column=col,
                    message=(
                        f"NativeApp-only property {prop!r} is not present in App.pkl.  "
                        + _k002_prop_hint(prop)
                        + "  Suppress with: // conformance: ignore[K002] <reason>"
                    ),
                    suppressed=suppressed,
                    suppression_justification=justification,
                )
            )

        # ── K002b: legacy import statements ──────────────────────────────
        # Skipped for credential-config sub-contracts, which legitimately import
        # Config.pkl for their widget types (see _K002_CREDENTIAL_BASE_RE).
        im = _K002_IMPORT_RE.search(code_only)
        if im and not base_is_credential:
            import_str = im.group(0)
            col = im.start() + 1
            suppressed, justification = _make_pkl_finding_suppressed(
                rule_id="K002", line=lineno, directives=directives
            )
            findings.append(
                Finding(
                    rule_id="K002",
                    file=rel,
                    line=lineno,
                    column=col,
                    message=(
                        f"Legacy import {import_str!r} is not needed in App.pkl.  "
                        + _k002_import_hint(import_str)
                        + "  Suppress with: // conformance: ignore[K002] <reason>"
                    ),
                    suppressed=suppressed,
                    suppression_justification=justification,
                )
            )

    return findings


def _k002_prop_hint(prop: str) -> str:
    """Return a one-sentence migration hint for a NativeApp-only property name."""
    if prop in ("flatManifestArgs", "manifestMetadataArgs"):
        return (
            "App.pkl always emits flat top-level manifest args — remove both "
            "flatManifestArgs and manifestMetadataArgs."
        )
    if prop == "workflowTypeOverride":
        return (
            "App.pkl takes a verbatim workflowType string — compute the final "
            "kebab-cased value and set it as workflowType, then remove "
            "workflowTypeOverride."
        )
    return "Remove this NativeApp.pkl-only property."


def _k002_import_hint(import_str: str) -> str:
    """Return a one-sentence migration hint for a legacy pkl import."""
    if "Config.pkl" in import_str:
        return (
            "App.pkl re-exports widget types as typealiases (UIConfig, TextInput, "
            "etc.) — remove this import and replace Config.* references with the "
            "unqualified names."
        )
    if "Credential.pkl" in import_str:
        return "Credential.pkl is an Argo-era module not used by App.pkl — remove this import."
    if "Renderers.pkl" in import_str:
        return "Renderers.pkl is an Argo-era module not used by App.pkl — remove this import."
    return "Remove this legacy import."


def discover(root: Path) -> list[Path]:
    """Return all ``contract/**/*.pkl`` files under *root*.

    Only the ``contract/`` directory is scanned.  Files outside that directory
    are toolkit sources or test fixtures, not app contracts.
    """
    contract_dir = root / "contract"
    if not contract_dir.is_dir():
        return []
    return sorted(contract_dir.rglob("*.pkl"))
