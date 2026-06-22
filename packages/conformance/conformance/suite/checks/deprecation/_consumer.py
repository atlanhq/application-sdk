"""B001 ``DeprecatedSdkSymbolUsage`` — flag app consumption of a deprecated symbol.

Runs against *consumer apps* (scope ``app``).  Reads the committed manifest and
flags three surfaces:

* **importing** a deprecated class/function — ``from application_sdk.x import Foo``;
* **subclassing** a deprecated base — ``class MyExtractor(BaseMetadataExtractor)``;
* **calling** a deprecated method by attribute — ``obj.upload_to_atlan(...)``.

**Module-aware matching.**  A class/function match requires both the symbol name
*and* the import module to agree with a manifest entry — because a deprecated
name can collide with its own recommended replacement.  The canonical case:
``application_sdk.app.AppError`` is deprecated and its notice says "use
``application_sdk.errors.AppError``" — same bare name.  Matching on name alone
would flag the *correct* migration target on every app.  So an import is only
flagged when the manifest entry's module is the imported module or a submodule of
it (``from application_sdk.app import AppError`` → matches ``app.base``;
``from application_sdk.errors import AppError`` → does not).  Method calls remain
attribute-name-anchored (a method is not importable), an accepted false-positive
risk at WARN.

The finding message carries the SDK's own migration guidance from the notice, so
the remediation loop can propose the concrete replacement.

Complement to E013 ``LegacyAtlanErrorRaise``: E013 owns the ``raise AtlanError``
site; B001 owns import / construct / subclass.  The surfaces do not overlap.

Coverage limits (intentional, documented): module-qualified *usage* without a
``from`` import (``import application_sdk.discovery as d; d.DiscoveryError(...)``)
and sibling re-export aliasing can produce false negatives — biased toward zero
false positives at WARN.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._manifest import SDK_IMPORT_ROOT, DeprecatedSymbol, Manifest

_RULE_ID = "B001"


def _hint(message: str) -> str:
    """Render the SDK's migration guidance as a trailing hint, if present."""
    return f" {message}" if message else ""


def _is_sdk_module(name: str) -> bool:
    return name == SDK_IMPORT_ROOT or name.startswith(SDK_IMPORT_ROOT + ".")


def _module_matches(import_mod: str, entry_module: str) -> bool:
    """True if a symbol defined in *entry_module* is reachable via *import_mod*.

    Exact module, or *entry_module* is a submodule of *import_mod* (covers
    re-export from a parent package).  Deliberately one-directional: importing
    from a *sibling* module (a different re-export) does not match.
    """
    return entry_module == import_mod or entry_module.startswith(import_mod + ".")


def scan_consumer(
    tree: ast.Module,
    file: str,
    manifest: Manifest,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Return B001 findings for *tree* against the deprecated-symbol *manifest*."""
    if not manifest.symbols:
        return []
    class_func: dict[str, list[DeprecatedSymbol]] = {}
    method: dict[str, DeprecatedSymbol] = {}
    for record in manifest.symbols:
        if record.kind in ("class", "function"):
            class_func.setdefault(record.symbol, []).append(record)
        elif record.kind == "method":
            method[record.symbol] = record

    findings: list[Finding] = []
    # Local name bound (via from-import) to a deprecated symbol's record.
    deprecated_bindings: dict[str, DeprecatedSymbol] = {}
    # Local alias bound to an application_sdk *module* (for `mod.Symbol` access),
    # mapped to that module's full dotted path.
    sdk_module_aliases: dict[str, str] = {}

    def _match_class_func(name: str, import_mod: str) -> DeprecatedSymbol | None:
        for record in class_func.get(name, ()):
            if _module_matches(import_mod, record.module):
                return record
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_sdk_module(alias.name):
                    continue
                if alias.asname:
                    sdk_module_aliases[alias.asname] = alias.name
                else:
                    # `import application_sdk.x` binds the top package name.
                    top = alias.name.split(".")[0]
                    sdk_module_aliases[top] = top
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if not _is_sdk_module(mod):
                continue
            for alias in node.names:
                record = _match_class_func(alias.name, mod)
                if record is None:
                    continue
                deprecated_bindings[alias.asname or alias.name] = record
                findings.append(
                    make_finding(
                        filename=file,
                        rule_id=_RULE_ID,
                        node=node,
                        message=(
                            f"Imports deprecated SDK symbol '{alias.name}' "
                            f"({record.module}).{_hint(record.message)}"
                        ),
                        directives=directives,
                    )
                )

    def _resolve_base(base: ast.expr) -> DeprecatedSymbol | None:
        if isinstance(base, ast.Name):
            return deprecated_bindings.get(base.id)
        if (
            isinstance(base, ast.Attribute)
            and isinstance(base.value, ast.Name)
            and base.value.id in sdk_module_aliases
        ):
            return _match_class_func(base.attr, sdk_module_aliases[base.value.id])
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                record = _resolve_base(base)
                if record is not None:
                    findings.append(
                        make_finding(
                            filename=file,
                            rule_id=_RULE_ID,
                            node=node,
                            message=(
                                f"Subclasses deprecated SDK symbol '{record.symbol}' "
                                f"({record.module}).{_hint(record.message)}"
                            ),
                            directives=directives,
                        )
                    )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in method
        ):
            record = method[node.func.attr]
            findings.append(
                make_finding(
                    filename=file,
                    rule_id=_RULE_ID,
                    node=node,
                    message=(
                        f"Calls deprecated SDK method '{record.symbol}' "
                        f"({record.module}).{_hint(record.message)}"
                    ),
                    directives=directives,
                )
            )

    return findings
