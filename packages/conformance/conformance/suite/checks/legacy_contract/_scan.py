"""K-series regex scanner — detect legacy contract-toolkit base modules and APIs.

Design notes
------------
This scanner operates on ``.pkl`` *source* (the contract files authored by app
developers), **not** on the ``pkl eval`` generated artifacts (``atlan.yaml``,
``app/generated/**``).  The distinction from P016/P025 is deliberate: the
App.pkl-vs-legacy difference lives entirely in the ``amends`` / ``extends`` /
``import`` lines and NativeApp-only property names, all of which ``pkl eval``
erases.

No ``pkl`` CLI is invoked here — just stdlib ``re``.  The scanner is therefore
safe to run in any environment, including CI runners that have no ``pkl``
installed.

Ways a contract reaches a legacy module (FND-5546)
--------------------------------------------------
The five legacy modules are ``NativeApp.pkl``, ``NativeAppBundle.pkl``,
``Config.pkl``, ``Credential.pkl`` and ``Renderers.pkl``.  A contract reaches
one of them by:

* ``amends "…/<legacy>.pkl"`` — any module spelling, including the
  ``package://…/app-contract-toolkit@<v>#/<legacy>.pkl`` URI form  (K001);
* ``extends "…/<legacy>.pkl"``  (K001);
* ``import "…/<legacy>.pkl" [as X]`` followed by an amend expression
  ``(X) { … }`` or ``new X { … }`` — the import line is K002, every
  ``(X) {`` / ``new X {`` is K001;
* a bare ``import "…/<legacy>.pkl"``  (K002).

Suppression
-----------
Pkl uses ``//`` for line comments.  The Python ``# conformance: ignore``
infrastructure cannot be reused; the pkl-specific parser lives in
``_ast_common/_directives_pkl.py``.

Comment stripping
-----------------
``//`` line comments and ``/* … */`` block comments are blanked before scanning
by a single string-aware pass (:func:`_strip_comments`), so a ``//`` inside a
``package://`` URI or a ``/*`` inside a string literal is not mistaken for a
comment.  Line and column positions are preserved.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _make_pkl_finding_suppressed,
    _parse_pkl_directives,
)
from conformance.suite.schema.findings import Finding

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# The five legacy toolkit modules that App.pkl superseded.  Widgets.pkl,
# Connectors.pkl, Deployment.pkl, Inputs.pkl and InputContract.pkl are current
# modules and must never be flagged.
_LEGACY_MODULES = ("NativeApp", "NativeAppBundle", "Config", "Credential", "Renderers")
_LEGACY_ALT = "|".join(_LEGACY_MODULES)

# K001 — the module is based on a legacy module (``amends`` or ``extends``).
# The ``(?:[^"]*/)?`` prefix requires the module name to sit immediately after a
# path separator (or at the start of the string), so "MyNativeApp.pkl" and
# "AppConfig.pkl" are not matched — only the exact legacy names.  It also
# covers the package-URI form ``package://…@0.39.2#/NativeApp.pkl``.
_K001_RE = re.compile(
    rf'\b(?:amends|extends)\s+"(?:[^"]*/)?(?P<module>{_LEGACY_ALT})\.pkl"',
)

# K002 — NativeApp-only property names and legacy imports.
# Split into two sub-patterns for precise column anchoring in the message.
_K002_PROP_RE = re.compile(
    r"\b(flatManifestArgs|manifestMetadataArgs|workflowTypeOverride)\b"
)
# Same anchoring as K001.  ``import*`` (glob import) is not a legacy shape.
#
# Connectors.pkl is deliberately NOT here: unlike Config/Credential/Renderers
# (Argo-era, dropped by App.pkl), the Connectors registry is still imported
# explicitly by consumers — App.pkl imports it internally and types `connector`
# as `Connectors.Type`, but does not re-export the constants, so every current
# toolkit example still `import`s it. Flagging it is a false positive.
_K002_IMPORT_RE = re.compile(
    rf'\bimport\s+"(?:[^"]*/)?(?P<module>{_LEGACY_ALT})\.pkl"'
    r"(?:\s+as\s+(?P<alias>[A-Za-z_]\w*))?"
)

# K002b exemption — a contract that *amends* Credential.pkl is a credential-config
# sub-contract, and legitimately imports Config.pkl for its widget types.
# Credential.pkl uses ``Config.*`` internally and, unlike App.pkl, does NOT
# re-export those widgets as unqualified typealiases, so the import is required
# for ``pkl eval`` on that base.  The amends line itself is a K001 finding; the
# Config.pkl import is not reported a second time.  The exemption is scoped to
# ``Config.pkl`` only: a ``Renderers.pkl`` / ``Credential.pkl`` import on such a
# contract is genuinely legacy and still fires K002b.
_K002_CREDENTIAL_BASE_RE = re.compile(r'\bamends\s+"(?:[^"]*/)?Credential\.pkl"')

# ---------------------------------------------------------------------------
# String-aware comment stripping
# ---------------------------------------------------------------------------

_STRING_START_RE = re.compile(r'(#*)("""|")')


def _strip_comments(text: str) -> str:
    """Blank ``//`` and ``/* … */`` comments outside string literals.

    Comment characters are replaced with spaces and newlines are kept, so the
    1-based line and column of every remaining character are unchanged.

    String literals are skipped verbatim: ``"…"`` (with ``\\`` escapes),
    ``\"\"\"…\"\"\"`` multi-line strings, and the custom-delimiter forms
    ``#"…"#`` / ``#\"\"\"…\"\"\"#``.  A single-line string that reaches a newline
    unterminated ends there, so one malformed line cannot swallow the file.
    Interpolations (``\\(…)``) are not parsed; a quote nested inside one ends the
    string early, which can only make the scan see more code, never hide any.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
            continue
        if c in ('"', "#"):
            m = _STRING_START_RE.match(text, i)
            if m:
                i = _skip_string(text, m, out)
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _skip_string(text: str, m: re.Match[str], out: list[str]) -> int:
    """Copy the string literal opened at *m* into *out*; return the end index."""
    hashes, quote = m.group(1), m.group(2)
    closing = quote + hashes
    multiline = quote == '"""'
    raw = bool(hashes)
    i = m.end()
    n = len(text)
    while i < n:
        if text.startswith(closing, i):
            i += len(closing)
            break
        ch = text[i]
        if ch == "\n" and not multiline:
            break
        if ch == "\\" and not raw:
            i += 2
            continue
        i += 1
    i = min(i, n)
    out.append(text[m.start() : i])
    return i


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def _finding(
    rule_id: str,
    rel: str,
    lineno: int,
    col: int,
    message: str,
    directives: dict,
) -> Finding:
    suppressed, justification = _make_pkl_finding_suppressed(
        rule_id=rule_id, line=lineno, directives=directives
    )
    return Finding(
        rule_id=rule_id,
        file=rel,
        line=lineno,
        column=col,
        message=f"{message}  Suppress with: // conformance: ignore[{rule_id}] <reason>",
        suppressed=suppressed,
        suppression_justification=justification,
    )


def scan_text(text: str, rel: str) -> list[Finding]:
    """Scan the pkl source *text* and return K-series findings.

    Parameters
    ----------
    text:
        Raw content of a contract ``.pkl`` file.
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

    clean = _strip_comments(text)
    lines = clean.splitlines()

    # A credential-config sub-contract (``amends Credential.pkl``) legitimately
    # imports Config.pkl for its widget types (see _K002_CREDENTIAL_BASE_RE).
    base_is_credential = bool(_K002_CREDENTIAL_BASE_RE.search(clean))

    # Names bound by a legacy import, so ``(Alias) { … }`` can be recognised as
    # amending that module.  Without ``as``, Pkl binds the module's own name.
    legacy_aliases: dict[str, str] = {
        im.group("alias") or im.group("module"): im.group("module")
        for im in _K002_IMPORT_RE.finditer(clean)
    }
    # ``(X) {`` amends the module; ``new X {`` instantiates it as a class —
    # both build a contract on the legacy base.  ``new X.FieldSpec {`` is a
    # nested class, not the module, and is not matched.
    alias_alt = "|".join(re.escape(a) for a in sorted(legacy_aliases))
    amend_expr_re = (
        re.compile(
            rf"(?:\(\s*(?P<alias>{alias_alt})\s*\)|\bnew\s+(?P<new_alias>{alias_alt}))"
            r"\s*\{"
        )
        if legacy_aliases
        else None
    )

    findings: list[Finding] = []

    for lineno, line in enumerate(lines, start=1):
        # ── K001: amends / extends a legacy module ────────────────────────
        m = _K001_RE.search(line)
        if m:
            module = f"{m.group('module')}.pkl"
            findings.append(
                _finding(
                    "K001",
                    rel,
                    lineno,
                    m.start() + 1,
                    f"Contract is based on the legacy module {module!r} — "
                    + _k001_hint(m.group("module"))
                    + "  Follow the K001 entry in the conformance package's "
                    "programs/areas/contract-toolkit.prose.md.",
                    directives,
                )
            )

        # ── K001: (Alias) { … } amends an imported legacy module ──────────
        if amend_expr_re is not None:
            for am in amend_expr_re.finditer(line):
                alias = am.group("alias") or am.group("new_alias")
                module = f"{legacy_aliases[alias]}.pkl"
                findings.append(
                    _finding(
                        "K001",
                        rel,
                        lineno,
                        am.start() + 1,
                        f"Object amends the legacy module {module!r} through "
                        f"its import alias ({alias}) — move this "
                        "contract into App.pkl's typed entrypoints block "
                        '(amends "@app-contract-toolkit/App.pkl").  Follow the '
                        "K001 entry in the conformance package's "
                        "programs/areas/contract-toolkit.prose.md.",
                        directives,
                    )
                )

        # ── K002a: NativeApp-only property names ──────────────────────────
        for pm in _K002_PROP_RE.finditer(line):
            prop = pm.group(1)
            findings.append(
                _finding(
                    "K002",
                    rel,
                    lineno,
                    pm.start() + 1,
                    f"NativeApp-only property {prop!r} is not present in App.pkl.  "
                    + _k002_prop_hint(prop),
                    directives,
                )
            )

        # ── K002b: legacy import statements ──────────────────────────────
        # Skipped only for the ``Config.pkl`` import on a credential-config
        # sub-contract (see _K002_CREDENTIAL_BASE_RE).
        im = _K002_IMPORT_RE.search(line)
        if im and not (base_is_credential and im.group("module") == "Config"):
            import_str = im.group(0)
            findings.append(
                _finding(
                    "K002",
                    rel,
                    lineno,
                    im.start() + 1,
                    f"Legacy import {import_str!r} is not needed in App.pkl.  "
                    + _k002_import_hint(im.group("module")),
                    directives,
                )
            )

    return findings


def _k001_hint(module: str) -> str:
    """Return a one-sentence migration hint for a legacy base module."""
    if module == "Credential":
        return (
            "the standalone credential base is replaced by App.pkl's "
            "additionalCredentialConfigs; fold this credential into the app's "
            "contract/app.pkl."
        )
    return 'migrate to App.pkl (amends "@app-contract-toolkit/App.pkl").'


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


def _k002_import_hint(module: str) -> str:
    """Return a one-sentence migration hint for a legacy pkl import."""
    if module == "Config":
        return (
            "App.pkl re-exports widget types as typealiases (UIConfig, TextInput, "
            "etc.) — remove this import and replace Config.* references with the "
            "unqualified names."
        )
    if module in ("NativeApp", "NativeAppBundle"):
        return (
            f"{module}.pkl is a legacy base — build each entrypoint in App.pkl's "
            "typed entrypoints block instead of amending this module, then remove "
            "this import."
        )
    return (
        f"{module}.pkl is an Argo-era module not used by App.pkl — remove this import."
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

# A PklProject dependency on the toolkit: either the conventional dependency key
# (``["app-contract-toolkit"]``) or a package URI naming the toolkit
# (``uri = "package://…/app-contract-toolkit@<version>"``).  The toolkit's own
# PklProject declares a ``baseUri`` and a ``name``, never a dependency, so it
# does not match.
_TOOLKIT_DEP_RE = re.compile(
    r'\[\s*"app-contract-toolkit"\s*\]|\buri\s*=\s*"package://[^"]*/app-contract-toolkit@'
)

# Directories never walked: VCS/tool state, virtualenvs, vendored JS, and
# generated output (e.g. ``contract/generated/input.pkl`` is toolkit output, not
# a contract the author wrote).
_PRUNE_DIRS = frozenset({"node_modules", "generated", "__pycache__"})


def _walk_pkl(base: Path) -> list[Path]:
    """Return every ``.pkl`` file under *base*, skipping pruned directories."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in _PRUNE_DIRS
        ]
        found.extend(Path(dirpath) / f for f in filenames if f.endswith(".pkl"))
    return found


def _depends_on_toolkit(pkl_project: Path) -> bool:
    try:
        text = pkl_project.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(_TOOLKIT_DEP_RE.search(_strip_comments(text)))


def discover(root: Path) -> list[Path]:
    """Return every contract ``.pkl`` file under *root*.

    Two sources are unioned:

    * ``contract/**/*.pkl`` — the SDK's contract layout, scanned whether or not
      it has a ``PklProject``;
    * every directory whose ``PklProject`` depends on the toolkit, wherever it
      sits in the repo (e.g. reference contracts under ``docs/``), walked
      recursively.

    Hidden directories, ``node_modules`` and ``generated`` directories are
    skipped.
    """
    files: set[Path] = set()
    contract_dir = root / "contract"
    if contract_dir.is_dir():
        files.update(_walk_pkl(contract_dir))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in _PRUNE_DIRS
        ]
        if "PklProject" in filenames and _depends_on_toolkit(
            Path(dirpath) / "PklProject"
        ):
            files.update(_walk_pkl(Path(dirpath)))
    return sorted(files)
