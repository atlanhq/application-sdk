"""K021 FilterFieldRejectsAeString — check implementation (CONNECT-1333 / -1389).

The type-aware sibling of K018. K018 verifies only that an ``include_*`` /
``exclude_*`` arg the Automation Engine sends is *declared* somewhere on the
entrypoint's Input contract — it says so in its own docstring, and it never
inspects the field's annotation or its validators. K021 covers exactly the gap
that leaves open.

**Why the type matters now.** Since contract-toolkit >= 0.9.0 the Automation
Engine renders filters as top-level flat JSON *strings* — ``'{}'``,
``'{"^db$": ["^schema$"]}'`` — not as structured objects. A contract that types
the field as a strict ``dict`` with no path to accept that string REJECTS the
payload, and the run crashes at Pydantic validation rather than falling back to
a default. That is the CONNECT-1333 / CONNECT-1389 class; the real offenders on
the fleet today are atlan-looker-app and atlan-fabric-app. (Contrast K018's
failure mode, which is *fail-open*: a dropped key silently defaults. K021's is
*fail-closed*: the whole workflow dies.)

**Acceptance — a filter field is SAFE if ANY of:**

1. **str union** — the (possibly ``Annotated``) annotation unions with ``str``:
   ``FilterMap | str``, ``dict[str, list[str]] | str``, ``str | dict[...]``.
   Pydantic then tries the ``str`` arm and the JSON string validates. Mixing in
   ``ExtractionInput`` *without* redeclaring the field is this path: the inherited
   ``FilterMap | str`` never appears as an in-repo ``AnnAssign``, so the rule
   stays silent. Redeclaring a strict ``dict`` is **not** safe — the live SDK
   coercer (``_is_sdk_sql_filter_field``) only runs when
   ``json_schema_extra == _FILTER_FIELD_JSON_SCHEMA_EXTRA``, and a type override
   drops that extra, so ``'{}'`` is rejected.
2. **own before-validator** — a ``@field_validator("<field>", ..., mode="before")``
   (or any ``mode="before"`` ``@model_validator``) on the app's own resolved
   in-repo class chain targets the field. This is the Sigma/Qlik app-local fix
   pattern: coerce the string yourself before field validation runs.

**A finding is emitted** when an ``include_*`` / ``exclude_*`` field IS declared
on the resolved entrypoint contract as an in-repo ``AnnAssign``, its annotation
is a (possibly ``Annotated``) ``dict`` with no ``str`` union, and no
before-validator targets it. One finding per offending field.

**No-op / stay-silent philosophy (mirrors K018).** Returns ``[]`` when
``app/generated/`` is absent, when no manifest binds the repo as a
contract-toolkit app, when no entrypoint Input contract can be resolved, or when
the contract's inheritance chain cannot be *fully* resolved (an unknown
third-party base). A false negative is always preferred over a false positive:
the rule is advisory (WARN), and its whole value is that a finding is actionable.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    make_finding,
)
from conformance.suite.checks._entrypoint_contract_classes import (
    CodeContractScan,
    scan_file_for_entrypoint_contracts,
)
from conformance.suite.checks._entrypoint_contract_fields import (
    _flatten_union,
    _normalize_type_node,
)
from conformance.suite.checks.entrypoint_alignment._contract_entrypoints import (
    scan_contract as scan_contract_entrypoints,
)
from conformance.suite.checks.prescriptions._decorator_provenance import (
    collect_import_provenance,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
)
from conformance.suite.schema.findings import Finding

# Reuse K018's chain-resolution machinery verbatim — same registries, same
# "prefer a false negative" resolution semantics.
from ._input_fields import _sole_extraction_input, _walk_chain

_RULE_ID = "K021"

# The filter fields the AE flattens to top-level JSON strings are named
# ``include_*`` / ``exclude_*`` across the fleet.
_FILTER_PREFIXES = ("include_", "exclude_")

# Type-alias names that stand in for a dict and so are still string-rejecting
# without a ``| str`` arm. ``FilterMap`` is the SDK's own
# ``dict[str, list[str]]`` alias — an app annotating a field ``FilterMap``
# (no union) rejects the AE string exactly as a bare ``dict`` would.
_DICT_ALIAS_NAMES = frozenset({"FilterMap"})


def _is_filter_field_name(name: str) -> bool:
    return name.startswith(_FILTER_PREFIXES)


def _union_arm_strings(annotation: ast.expr) -> list[str]:
    """Top-level union arms of *annotation* as canonical strings.

    Uses K018's field-resolver normaliser, which strips ``Annotated[...]``,
    rewrites ``Optional``/``Union``/``X | None`` to canonical ``|`` form, and
    lowercases typing aliases (``Dict`` -> ``dict``). So ``Annotated[dict | str,
    m]`` -> ``["dict", "str"]`` and ``dict[str, Any]`` -> ``["dict[str, Any]"]``
    (the inner ``str`` is not a top-level arm).
    """
    normalized = _normalize_type_node(annotation)
    return [ast.unparse(arm) for arm in _flatten_union(normalized)]


def _unions_str(annotation: ast.expr) -> bool:
    """True when a top-level arm is exactly ``str`` (acceptance #1)."""
    return "str" in _union_arm_strings(annotation)


def _is_dict_typed(annotation: ast.expr) -> bool:
    """True when a top-level arm is a ``dict`` (bare, subscripted, or a known
    dict alias such as ``FilterMap``).

    Only a ``dict``-typed field can reject the AE's JSON *string*; a filter-named
    field of some other type (a ``bool`` toggle, say) is out of scope.
    """
    for arm in _union_arm_strings(annotation):
        if arm == "dict" or arm.startswith("dict[") or arm in _DICT_ALIAS_NAMES:
            return True
    return False


def _decorator_call(dec: ast.expr) -> ast.Call | None:
    """The ``ast.Call`` form of a decorator, or ``None`` for a bare name."""
    return dec if isinstance(dec, ast.Call) else None


def _decorator_base_name(dec: ast.expr) -> str | None:
    """Simple name of a decorator (``field_validator`` / ``model_validator``),
    whether written bare, called, or attribute-qualified (``pydantic.field_validator``)."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _is_before_mode(call: ast.Call) -> bool:
    return any(
        kw.arg == "mode"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value == "before"
        for kw in call.keywords
    )


def _field_validator_targets(call: ast.Call) -> set[str]:
    """Field names a ``@field_validator(...)`` call targets.

    The positional string args are field names; ``"*"`` targets every field.
    """
    targets: set[str] = set()
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            targets.add(arg.value)
    return targets


def _has_before_validator(chain_nodes: list[ast.ClassDef], field_name: str) -> bool:
    """True when *field_name* is coerced before field validation by the app's own
    resolved chain (acceptance #2).

    A ``mode="before"`` ``@model_validator`` covers every field; a
    ``mode="before"`` ``@field_validator`` covers the field only when it names it
    (or uses the ``"*"`` wildcard).
    """
    for classdef in chain_nodes:
        for stmt in classdef.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in stmt.decorator_list:
                base = _decorator_base_name(dec)
                if base not in ("field_validator", "model_validator"):
                    continue
                call = _decorator_call(dec)
                if call is None or not _is_before_mode(call):
                    continue
                if base == "model_validator":
                    return True
                targets = _field_validator_targets(call)
                if field_name in targets or "*" in targets:
                    return True
    return False


def _effective_filter_fields(
    chain_nodes: list[ast.ClassDef],
) -> list[tuple[str, ast.AnnAssign, ast.ClassDef]]:
    """Filter fields declared across *chain_nodes*, honouring MRO overrides.

    ``chain_nodes`` is ordered child-first (see :func:`_walk_chain`), so the
    first annotation seen for a field name is the effective one — a subclass that
    redeclares a base field wins, exactly as Python resolves it. Each entry is
    ``(field_name, annotation-assignment, declaring-class)``; the declaring class
    lets the caller anchor the finding on the right file/line.
    """
    seen: dict[str, tuple[str, ast.AnnAssign, ast.ClassDef]] = {}
    for classdef in chain_nodes:
        for stmt in classdef.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            if not isinstance(stmt.target, ast.Name):
                continue
            name = stmt.target.id
            if name in seen or not _is_filter_field_name(name):
                continue
            seen[name] = (name, stmt, classdef)
    return list(seen.values())


def _resolve_input_contracts(
    code: CodeContractScan,
    by_name: dict[str, ClassRecord],
    trees: dict[str, ast.AST],
    mode: str,
) -> list[ClassRecord]:
    """The entrypoint Input contract(s) to inspect, de-duplicated by class name.

    Two resolution paths, mirroring K018:

    1. explicit ``@entrypoint`` methods in the app's own source name their Input
       contract (``EntrypointContract.input_class_name``); or
    2. the app inherits its entrypoint from an SDK template (no decorator to
       read) — fall back to the app's sole ``ExtractionInput`` descendant, which
       is single-entrypoint-mode only. Kept so resolution matches K018 exactly
       rather than diverging silently. A bare subclass with no in-repo filter
       ``AnnAssign`` stays silent; a redeclared strict ``dict`` still fires.
    """
    recs: dict[str, ClassRecord] = {}
    if code.entrypoints:
        for ep in code.entrypoints:
            if ep.input_class_name is None:
                continue
            rec = by_name.get(ep.input_class_name)
            if rec is not None:
                recs.setdefault(rec.name, rec)
        return list(recs.values())

    if mode != "single":
        return []
    rec = _sole_extraction_input(by_name, trees)
    return [rec] if rec is not None else []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Flag entrypoint filter fields that cannot accept the AE's flat JSON string.

    No-ops when ``app/generated/`` is absent, when the generated tree binds no
    manifest, when no entrypoint Input contract resolves, or when a contract's
    inheritance chain cannot be fully resolved (stay silent rather than guess).
    """
    if not (root / "app" / "generated").is_dir():
        return []

    contract = scan_contract_entrypoints(root)
    if contract.mode == "absent":
        return []

    file_trees: dict[str, ast.AST] = {}
    file_directives: dict[str, dict[int, _IgnoreDirective]] = {}
    file_aliases: dict[str, dict[str, str]] = {}
    by_name: dict[str, ClassRecord] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)

        file_trees[rel] = tree
        file_directives[rel] = _parse_directives(text)
        aliases = collect_import_aliases(tree) if isinstance(tree, ast.Module) else {}
        file_aliases[rel] = aliases
        for rec in collect_classes(tree, rel, aliases):
            by_name.setdefault(rec.name, rec)

    code = CodeContractScan()
    app_cache: dict[str, bool | None] = {}
    for rel, tree in file_trees.items():
        if not isinstance(tree, ast.Module):
            continue
        prov = collect_import_provenance(tree)
        scan_file_for_entrypoint_contracts(
            tree, rel, file_aliases.get(rel, {}), prov, by_name, app_cache, code
        )

    input_recs = _resolve_input_contracts(code, by_name, file_trees, contract.mode)

    findings: list[Finding] = []
    for input_rec in input_recs:
        chain_nodes, fully_resolved = _walk_chain(input_rec, by_name)
        if not fully_resolved:
            continue  # incomplete picture — stay silent rather than guess.

        directives = file_directives.get(input_rec.file, {})
        for name, ann_node, _declaring in _effective_filter_fields(chain_nodes):
            annotation = ann_node.annotation
            if _unions_str(annotation):
                continue  # acceptance #1
            if not _is_dict_typed(annotation):
                continue  # a non-dict filter-named field cannot reject the string
            if _has_before_validator(chain_nodes, name):
                continue  # acceptance #2
            findings.append(_make_finding(input_rec, name, directives))

    return findings


def _make_finding(
    input_rec: ClassRecord,
    field_name: str,
    directives: dict[int, _IgnoreDirective],
) -> Finding:
    # Anchor on the entrypoint contract class, exactly as K018 does: the
    # suppression directive lives on the Input class definition (see the message),
    # and a field inherited from an in-repo ancestor lives in a different file
    # whose lineno would be attributed to the wrong file if reused here. The
    # field name travels on the discriminator so several findings on one class
    # stay distinct fingerprints.
    return dataclasses.replace(
        make_finding(
            filename=input_rec.file,
            rule_id=_RULE_ID,
            node=input_rec.node,
            message=(
                f"'{input_rec.name}' declares filter field '{field_name}' as a strict "
                "dict with no way to accept a string. Since contract-toolkit 0.9.0 the "
                "Automation Engine sends include/exclude filters as flat top-level JSON "
                "strings (e.g. '{}' or '{\"^db$\": [\"^schema$\"]}'), so Pydantic "
                f"rejects the payload and the workflow crashes at validation "
                "(CONNECT-1333 / CONNECT-1389). Make the field accept the string in any "
                "one of these ways: (1) union the annotation with str — "
                f"'{field_name}: FilterMap | str' (or 'dict[str, list[str]] | str'); "
                "(2) mix in "
                "'application_sdk.templates.contracts.sql_metadata.ExtractionInput' "
                "*without* redeclaring a strict dict — a type override drops the SDK "
                "json_schema_extra the coercer keys on, so the inherited "
                "'_coerce_filter' does not run; or (3) add a "
                f"@field_validator('{field_name}', mode='before') to this contract that "
                "coerces the string yourself. Note an 'after'-mode validator runs too "
                "late — the string is already rejected. Suppress with "
                f"'# conformance: ignore[{_RULE_ID}] <reason>' on the contract class "
                "definition."
            ),
            directives=directives,
        ),
        discriminator=field_name,
    )
