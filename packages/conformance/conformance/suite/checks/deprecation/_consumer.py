"""B001 ``DeprecatedSdkSymbolUsage`` — flag app consumption of a deprecated symbol.

Runs against *consumer apps* (scope ``app``).  Reads the committed manifest and
flags four surfaces:

* **importing** a deprecated class/function — ``from application_sdk.x import Foo``;
* **subclassing** a deprecated base — ``class MyExtractor(BaseMetadataExtractor)``;
* **calling** a deprecated method by attribute — ``obj.upload_to_atlan(...)``;
* **reading** a deprecated enum member — ``DataframeType.daft``.

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

Enum members are module-aware too, via the enum class they hang off: the member
is only matched when its enum was imported from a module the manifest entry
agrees with, so an app's own ``DataframeType`` never picks up the SDK's
deprecated members.

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
    # Deprecated enum members, keyed by the enum they belong to:
    # ``{"DataframeType": {"daft": <record>}}``.  The manifest stores them
    # qualified (``DataframeType.daft``) because the bare member name is not
    # importable and would collide across enums.
    enum_member: dict[str, dict[str, DeprecatedSymbol]] = {}
    for record in manifest.symbols:
        if record.kind in ("class", "function"):
            class_func.setdefault(record.symbol, []).append(record)
        elif record.kind == "method":
            method[record.symbol] = record
        elif record.kind == "enum_member" and "." in record.symbol:
            enum_name, _, member = record.symbol.rpartition(".")
            enum_member.setdefault(enum_name, {})[member] = record

    findings: list[Finding] = []
    # Local name bound (via from-import) to a deprecated symbol's record.
    deprecated_bindings: dict[str, DeprecatedSymbol] = {}
    # Local alias bound to an application_sdk *module* (for `mod.Symbol` access),
    # mapped to that module's full dotted path.
    sdk_module_aliases: dict[str, str] = {}
    # Local name bound to an enum class that has deprecated members, mapped to
    # that enum's name in the manifest.
    enum_bindings: dict[str, str] = {}

    def _match_class_func(name: str, import_mod: str) -> DeprecatedSymbol | None:
        for record in class_func.get(name, ()):
            if _module_matches(import_mod, record.module):
                return record
        return None

    def _enum_declares_members(name: str, import_mod: str) -> bool:
        """Whether *name*, imported from *import_mod*, is a manifest enum.

        Module-aware for the same reason class/function matching is: an app's
        own ``DataframeType`` must not pick up the SDK's deprecated members.
        """
        return any(
            _module_matches(import_mod, record.module)
            for record in enum_member.get(name, {}).values()
        )

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
                if _enum_declares_members(alias.name, mod):
                    enum_bindings[alias.asname or alias.name] = alias.name
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

    def _resolve_enum_member(node: ast.Attribute) -> DeprecatedSymbol | None:
        """Resolve ``Enum.member`` access to a deprecated-member record.

        Covers both spellings the import loop can bind: a directly-imported enum
        (``DataframeType.daft``) and a module-qualified one
        (``types.DataframeType.daft``).
        """
        receiver = node.value
        if isinstance(receiver, ast.Name):
            enum_name = enum_bindings.get(receiver.id)
        elif (
            isinstance(receiver, ast.Attribute)
            and isinstance(receiver.value, ast.Name)
            and receiver.value.id in sdk_module_aliases
            and _enum_declares_members(
                receiver.attr, sdk_module_aliases[receiver.value.id]
            )
        ):
            enum_name = receiver.attr
        else:
            return None
        if enum_name is None:
            return None
        return enum_member.get(enum_name, {}).get(node.attr)

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
        elif isinstance(node, ast.Attribute):
            record = _resolve_enum_member(node)
            if record is not None:
                findings.append(
                    make_finding(
                        filename=file,
                        rule_id=_RULE_ID,
                        node=node,
                        message=(
                            f"Uses deprecated SDK enum member '{record.symbol}' "
                            f"({record.module}).{_hint(record.message)}"
                        ),
                        directives=directives,
                    )
                )

    return findings
