"""B005 NonAdditiveContractChange / B006 StaleContractLedger — AST-based checker.

Entrypoint-only scope: only Input/Output contracts referenced by ``@entrypoint``
methods or implicit ``App.run()`` are gated.  ``@task`` contracts are excluded.

Entrypoint-contract discovery and field extraction (including the full
inheritance-hierarchy walk) live in the neutral
``suite.checks._entrypoint_contract_fields`` module — shared with the ledger
generator, and expected to also back the K-series contract-toolkit checks.
This module owns only the B005/B006 finding-emission logic: comparing
resolved live fields against the committed ledger.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    collect_module_alias_targets,
    make_finding,
    register_alias_records,
)
from conformance.suite.checks._entrypoint_contract_fields import (
    _canonical_type,
    collect_entrypoint_contract_names,
    resolve_contract_fields,
    sdk_base_contract_names,
    sdk_contract_ancestors,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
)
from conformance.suite.schema.disposition import RuleScope
from conformance.suite.schema.findings import Finding

from ._ledger_schema import (
    ContractField,
    ContractLedger,
    load_sdk_ledger,
    regen_command,
)
from ._sdk_type_aliases import TypeAliasDef, collect_sdk_imported_aliases

# ── Main scan function ────────────────────────────────────────────────────────


def _split_union(canonical: str) -> frozenset[str]:
    """Union members of a canonical type string, split at bracket depth 0.

    ``"dict[str, Any] | None"`` -> ``{"dict[str, Any]", "None"}``. Splitting
    naively on ``|`` would tear ``dict[str, int | None]`` apart.
    """
    members, depth, cur = [], 0, ""
    for ch in canonical:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "|" and depth == 0:
            members.append(cur)
            cur = ""
            continue
        cur += ch
    members.append(cur)
    return frozenset(m.strip() for m in members if m.strip())


# Structural view of a canonical type string. Canonical forms are produced by
# ast.unparse after _canonical_type, so they round-trip through ast.parse.


@dataclass(frozen=True, slots=True)
class _TName:
    name: str


@dataclass(frozen=True, slots=True)
class _TApp:
    ctor: str
    args: tuple[_TNode, ...]


@dataclass(frozen=True, slots=True)
class _TUnion:
    members: frozenset[_TNode]


_TNode = _TName | _TApp | _TUnion


def _ctor_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return ast.unparse(node)


def _node_from_ast(node: ast.expr) -> _TNode:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        parts: list[_TNode] = []

        def flatten(n: ast.expr) -> None:
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
                flatten(n.left)
                flatten(n.right)
            else:
                parts.append(_node_from_ast(n))

        flatten(node)
        members: set[_TNode] = set()
        for part in parts:
            if isinstance(part, _TUnion):
                members.update(part.members)
            else:
                members.add(part)
        if len(members) == 1:
            return next(iter(members))
        return _TUnion(frozenset(members))
    if isinstance(node, ast.Subscript):
        sl = node.slice
        args = (
            tuple(_node_from_ast(e) for e in sl.elts)
            if isinstance(sl, ast.Tuple)
            else (_node_from_ast(sl),)
        )
        return _TApp(_ctor_name(node.value), args)
    if isinstance(node, ast.Name):
        return _TName(node.id)
    if isinstance(node, ast.Constant) and node.value is None:
        return _TName("None")
    if isinstance(node, ast.Constant) and node.value is Ellipsis:
        return _TName("...")
    return _TName(ast.unparse(node))


def _parse_canonical(canonical: str) -> _TNode:
    try:
        tree = ast.parse(canonical, mode="eval")
    except SyntaxError:
        return _TName(canonical)
    return _node_from_ast(tree.body)


def _contains_any(node: _TNode) -> bool:
    if isinstance(node, _TName):
        return node.name == "Any"
    if isinstance(node, _TApp):
        return any(_contains_any(a) for a in node.args)
    return any(_contains_any(m) for m in node.members)


def _is_subtype(old: _TNode, new: _TNode) -> bool:
    """True when every payload that validated as *old* still validates as *new*.

    ``Any`` is not treated as a top type: moving onto ``Any`` is not a
    widening (P001 forbids it as a destination, and it is not producer-safe
    in the other direction either).
    """
    if old == new:
        return True
    if isinstance(old, _TUnion):
        return all(_is_subtype(m, new) for m in old.members)
    if isinstance(new, _TUnion):
        return any(_is_subtype(old, m) for m in new.members)
    if isinstance(old, _TApp) and isinstance(new, _TApp):
        if old.ctor != new.ctor or len(old.args) != len(new.args):
            return False
        return all(_is_subtype(a, b) for a, b in zip(old.args, new.args, strict=True))
    return False


def _is_widening(old: str, new: str) -> bool:
    """True when *new* accepts everything *old* did, and more.

    Recurses into parameterized containers, so ``list[str]`` →
    ``list[str | None]`` is a widening the same way ``str`` → ``str | None``
    is. A top-level union-set comparison would treat those as unrelated
    strings and flag a producer-safe change as a break.
    """
    old_n, new_n = _parse_canonical(old), _parse_canonical(new)
    return old_n != new_n and _is_subtype(old_n, new_n)


def _union_members(node: _TNode) -> frozenset[_TNode]:
    if isinstance(node, _TUnion):
        return node.members
    return frozenset({node})


def _match_union(old_ms: list[_TNode], new_ms: list[_TNode]) -> bool:
    """True when every old arm pairs with a distinct new arm (Any may match any)."""
    if not old_ms:
        return not new_ms
    o, rest = old_ms[0], old_ms[1:]
    for i, n in enumerate(new_ms):
        if _any_replaced_in_place(o, n) and _match_union(
            rest, new_ms[:i] + new_ms[i + 1 :]
        ):
            return True
    return False


def _any_replaced_in_place(old: _TNode, new: _TNode) -> bool:
    """True when *new* is *old* with ``Any`` replaced at the same positions.

    Same constructor and same union arms required: ``dict[str, Any]`` →
    ``dict[str, str]`` is the P001-required migration, but ``dict[str, Any]``
    → ``list[str]`` is a payload break.
    """
    if isinstance(old, _TName) and old.name == "Any":
        return True
    if isinstance(old, _TUnion) or isinstance(new, _TUnion):
        old_ms, new_ms = list(_union_members(old)), list(_union_members(new))
        if len(old_ms) != len(new_ms):
            return False
        return _match_union(old_ms, new_ms)
    if isinstance(old, _TApp) and isinstance(new, _TApp):
        if old.ctor != new.ctor or len(old.args) != len(new.args):
            return False
        return all(
            _any_replaced_in_place(a, b)
            for a, b in zip(old.args, new.args, strict=True)
        )
    return old == new


def _retype_is_compatible(
    ledger_type: str, live_type: str, *, inherited: bool
) -> str | None:
    """Why this retype is not a break, or None if it genuinely might be."""
    if inherited:
        return (
            "the field is inherited and its type is set by the base class, so "
            "this app did not make the change and cannot revert it"
        )
    if _is_widening(ledger_type, live_type):
        return (
            "the type was widened, so every payload that validated against the "
            "recorded type still validates"
        )
    old_n, new_n = _parse_canonical(ledger_type), _parse_canonical(live_type)
    if (
        _contains_any(old_n)
        and not _contains_any(new_n)
        and _any_replaced_in_place(old_n, new_n)
    ):
        return (
            "the recorded type contained Any, which payload-safety (P001) "
            "refuses at class-definition time — moving to a concrete type in "
            "the same outer shape is required, not optional"
        )
    return None


def _is_type_alias_call(node: ast.Call) -> bool:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name == "TypeAliasType"


_TYPE_VAR_FACTORIES = frozenset({"TypeVar", "ParamSpec", "TypeVarTuple"})


def _collect_type_vars(tree: ast.Module) -> frozenset[str]:
    """Names bound at module level by ``X = TypeVar(...)`` and its siblings."""
    names: set[str] = set()
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Call)
        ):
            func = stmt.value.func
            name = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            )
            if name in _TYPE_VAR_FACTORIES:
                names.add(stmt.targets[0].id)
    return frozenset(names)


def _free_type_vars(value: ast.expr, type_vars: frozenset[str]) -> tuple[str, ...]:
    """Type variables in *value*, in order of first appearance (typing's rule)."""
    seen: list[str] = []
    for node in ast.walk(value):
        if isinstance(node, ast.Name) and node.id in type_vars and node.id not in seen:
            seen.append(node.id)
    return tuple(seen)


def _explicit_params(nodes: list[ast.expr] | list[ast.AST]) -> tuple[str, ...]:
    names: list[str] = []
    for node in nodes:
        name = getattr(node, "name", None) or getattr(node, "id", None)
        if isinstance(name, str):
            names.append(name)
    return tuple(names)


def collect_type_aliases(tree: ast.AST) -> dict[str, TypeAliasDef]:
    """Module-level type aliases, mapped to the expression they stand for.

    Recognizes ``X = TypeAliasType("X", <expr>)``, ``X: TypeAlias = <expr>``,
    ``type X = <expr>``, and a plain ``X = <expr>`` whose value is a subscript
    or ``|`` union (the only plain assignments that are unambiguously types).
    Type parameters come from ``type X[K, V]`` / ``type_params=(K, V)`` when
    declared, else from the module's ``TypeVar``s in order of first appearance.
    """
    aliases: dict[str, TypeAliasDef] = {}
    if not isinstance(tree, ast.Module):
        return aliases
    type_vars = _collect_type_vars(tree)

    def implicit(value: ast.expr) -> TypeAliasDef:
        return TypeAliasDef(value, _free_type_vars(value, type_vars))

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                continue
            name, value = stmt.targets[0].id, stmt.value
            if isinstance(value, ast.Call) and _is_type_alias_call(value):
                target = value.args[1] if len(value.args) > 1 else None
                declared: tuple[str, ...] | None = None
                for kw in value.keywords:
                    if kw.arg == "value":
                        target = kw.value
                    elif kw.arg == "type_params" and isinstance(
                        kw.value, (ast.Tuple, ast.List)
                    ):
                        declared = _explicit_params(kw.value.elts)
                if target is not None:
                    aliases[name] = (
                        TypeAliasDef(target, declared)
                        if declared is not None
                        else implicit(target)
                    )
            elif isinstance(value, ast.Subscript) or (
                isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr)
            ):
                aliases[name] = implicit(value)
        elif isinstance(stmt, ast.AnnAssign):
            if (
                isinstance(stmt.target, ast.Name)
                and stmt.value is not None
                and (
                    (
                        isinstance(stmt.annotation, ast.Name)
                        and stmt.annotation.id == "TypeAlias"
                    )
                    or (
                        isinstance(stmt.annotation, ast.Attribute)
                        and stmt.annotation.attr == "TypeAlias"
                    )
                )
            ):
                aliases[stmt.target.id] = implicit(stmt.value)
        elif isinstance(stmt, getattr(ast, "TypeAlias", ())) and isinstance(
            stmt.name, ast.Name
        ):
            aliases[stmt.name.id] = TypeAliasDef(
                stmt.value, _explicit_params(getattr(stmt, "type_params", []))
            )
    return aliases


def _binding_counts(tree: ast.AST) -> tuple[dict[str, int], bool]:
    """How many times each name is bound anywhere in *tree*, and any star import.

    Deliberately flat: every scope counts — a function local, a class
    attribute, a walrus in a default — with no attempt to decide which binding
    a given annotation resolves to.
    """
    counts: dict[str, int] = {}
    star = False

    def bump(name: str | None) -> None:
        if name:
            counts[name] = counts.get(name, 0) + 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bump(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bump(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    star = True
                else:
                    bump(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            bump(node.name)
    return counts, star


def _unshadowed(
    aliases: dict[str, TypeAliasDef], tree: ast.AST
) -> dict[str, TypeAliasDef]:
    """Keep only aliases whose name is bound exactly once in the file.

    Any other binding of the name anywhere in the file, or any
    ``from x import *``, disables expansion.  Modelling which binding a given
    annotation actually sees means re-implementing Python's name resolution,
    and every approximation either hides a real break or blocks a valid fix.
    This rule only errs toward the second, and there B005 behaves exactly as
    it did before alias expansion existed.  Runtime writes such as
    ``globals()["X"] = ...`` are not visible to any static rule.
    """
    counts, star = _binding_counts(tree)
    if star:
        return {}
    return {name: a for name, a in aliases.items() if counts.get(name, 0) == 1}


_ALIAS_EXPANSION_BUDGET = 2_000


class _AliasBudgetExceeded(Exception):
    pass


class _Substitute(ast.NodeTransformer):
    def __init__(self, bindings: dict[str, ast.expr]) -> None:
        self._bindings = bindings

    def visit_Name(self, node: ast.Name) -> ast.expr:
        bound = self._bindings.get(node.id)
        return copy.deepcopy(bound) if bound is not None else node


class _AliasExpander(ast.NodeTransformer):
    def __init__(
        self,
        aliases: dict[str, TypeAliasDef],
        expanding: frozenset[str] = frozenset(),
        spent: list[int] | None = None,
    ) -> None:
        self._aliases = aliases
        self._expanding = expanding
        self._spent = spent if spent is not None else [0]

    def _expand(self, name: str, args: list[ast.expr]) -> ast.expr:
        alias = self._aliases[name]
        self._spent[0] += sum(1 for _ in ast.walk(alias.value))
        if self._spent[0] > _ALIAS_EXPANSION_BUDGET:
            raise _AliasBudgetExceeded
        expanded = _AliasExpander(
            self._aliases, self._expanding | {name}, self._spent
        ).visit(copy.deepcopy(alias.value))
        if not alias.params:
            return expanded
        fill = args or [ast.Name(id="Any", ctx=ast.Load()) for _ in alias.params]
        return _Substitute(dict(zip(alias.params, fill, strict=True))).visit(expanded)

    def _expandable(self, node: ast.expr) -> str | None:
        if not isinstance(node, ast.Name) or node.id in self._expanding:
            return None
        return node.id if node.id in self._aliases else None

    def visit_Subscript(self, node: ast.Subscript) -> ast.expr:
        name = self._expandable(node.value)
        if name is None or not self._aliases[name].params:
            visited = self.generic_visit(node)
            return visited if isinstance(visited, ast.expr) else node
        raw = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if len(raw) != len(self._aliases[name].params):
            return node
        return self._expand(name, [self.visit(arg) for arg in raw])

    def visit_Name(self, node: ast.Name) -> ast.expr:
        name = self._expandable(node)
        return node if name is None else self._expand(name, [])


def _expand_aliases(
    annotation: ast.expr, aliases: dict[str, TypeAliasDef]
) -> str | None:
    """Canonical type of *annotation* with same-module aliases expanded.

    Alias chains are followed; an alias already being expanded is left as its
    name, so self- and mutually-referential aliases terminate.  A generic
    alias takes its subscript's arguments (``BoundedDict[str, int]``), or
    ``Any`` for each parameter when used bare; an argument count that does not
    match its parameters leaves the subscript unexpanded.  An expansion
    that grows past ``_ALIAS_EXPANSION_BUDGET`` nodes is abandoned and the
    annotation is compared unexpanded, so a dense chain cannot blow up.
    """
    if not aliases:
        return None
    try:
        expanded = _AliasExpander(aliases).visit(copy.deepcopy(annotation))
    except _AliasBudgetExceeded:
        return None
    canonical = _canonical_type(expanded)
    return canonical if canonical != _canonical_type(annotation) else None


def scan_contract_compat(
    paths: list[Path],
    root: Path,
    ledger: ContractLedger,
    scope: RuleScope | None = None,
    *,
    sdk_ledger: ContractLedger | None = None,
) -> list[Finding]:
    """Emit B005/B006 for entrypoint contract backwards-compatibility violations.

    *sdk_ledger* is the SDK's own ledger (default: the copy bundled in this
    package). A field it records 'sunset' on an SDK contract this contract
    still inherits from was retired by the SDK, so its absence is not B005.

    Two-pass:
    1. Parse every file; build the cross-file class registry (needed for
       App-subclass resolution, which determines whether ``run()`` is an
       implicit entrypoint).
    2. For each file, check every entrypoint-contract class against the ledger.
    """
    # Pass 1: parse + build class registry
    file_trees: dict[Path, ast.AST] = {}
    file_directives: dict[Path, dict[int, _IgnoreDirective]] = {}
    file_aliases: dict[Path, dict[str, str]] = {}
    file_type_aliases: dict[Path, dict[str, TypeAliasDef]] = {}
    by_name: dict[str, ClassRecord] = {}
    # Every declaration per class name, not just the first. The ledger keys
    # fields by BARE class name, so a name declared in two modules makes the
    # ledger ambiguous — see the B005 presence check below.
    by_name_all: dict[str, list[ClassRecord]] = {}
    aliases_by_rel: dict[str, dict[str, str]] = {}
    # Module-level rebindings (``OpenAPIConnectorInput = AppInputContract``),
    # merged into the class registry once every file has been parsed so a
    # rebinding declared in one module resolves a class defined in another.
    alias_targets: dict[str, str] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        file_trees[path] = tree
        file_directives[path] = _parse_directives(text)

        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        aliases = collect_import_aliases(tree) if isinstance(tree, ast.Module) else {}
        file_aliases[path] = aliases
        file_type_aliases[path] = _unshadowed(
            {**collect_sdk_imported_aliases(tree), **collect_type_aliases(tree)},
            tree,
        )
        aliases_by_rel[rel] = aliases
        for rec in collect_classes(tree, rel, aliases):
            by_name.setdefault(rec.name, rec)
            by_name_all.setdefault(rec.name, []).append(rec)
        for local, target in collect_module_alias_targets(tree, aliases).items():
            alias_targets.setdefault(local, target)

    # Only ``by_name`` is seeded: an alias is another name for a declaration
    # already in ``by_name_all``, not a second declaration, and adding it there
    # would make every aliased contract read as an ambiguous name.
    register_alias_records(by_name, alias_targets)

    entrypoint_names = collect_entrypoint_contract_names(
        file_trees, by_name
    ) | sdk_base_contract_names(by_name)

    if not entrypoint_names:
        return []

    # Pre-index the ledger for O(1) lookups
    ledger_by_key: dict[tuple[str, str], ContractField] = {
        (f.contract, f.field): f for f in ledger.fields
    }
    ledger_by_contract: dict[str, list[ContractField]] = {}
    for f in ledger.fields:
        ledger_by_contract.setdefault(f.contract, []).append(f)

    sdk_retired: dict[str, set[tuple[str, str]]] = {}
    for f in (sdk_ledger if sdk_ledger is not None else load_sdk_ledger()).fields:
        if f.status == "sunset":
            sdk_retired.setdefault(f.contract, set()).add((f.field, f.type))

    regen = regen_command(scope)
    has_ambiguous_names = any(len(v) > 1 for v in by_name_all.values())

    findings: list[Finding] = []

    # Pass 2: per-file contract checks
    for path, tree in file_trees.items():
        directives = file_directives.get(path, {})
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)

        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue
            if class_node.name not in entrypoint_names:
                continue

            aliases = file_aliases.get(path, {})
            live_fields = resolve_contract_fields(class_node, aliases, by_name)
            live_by_name = {f.name: f for f in live_fields}

            # A ledger entry is keyed by BARE class name. When that name is
            # declared more than once in the repo — an app whose crawler and
            # miner entrypoints both declare `AppInputContract`, or a contract
            # whose base resolves to one of them — the entry cannot be
            # attributed to a single declaration, and every field belonging to
            # the OTHER declaration reads as "removed from the contract".
            # (Live: 21 of clickhouse's 25 B005 findings were exactly this.)
            # So compute presence against the union of same-named declarations
            # and ambiguity-aware ancestors. Presence ONLY: `live_fields`
            # itself is untouched, so B006 and the type-change check below keep
            # today's behaviour exactly.
            present_names = set(live_by_name)
            if class_node.name == _LEGACY_BUNDLE_INPUT:
                present_names.update(
                    f.name
                    for path_, node in _renamed_bundle_inputs(file_trees)
                    for f in resolve_contract_fields(
                        node,
                        file_aliases.get(path_, {}),
                        by_name,
                        by_name_all=by_name_all,
                    )
                )
            if has_ambiguous_names:
                present_names.update(
                    f.name
                    for f in resolve_contract_fields(
                        class_node, aliases, by_name, by_name_all=by_name_all
                    )
                )
                for rec in by_name_all.get(class_node.name, []):
                    if rec.node is class_node:
                        continue
                    present_names.update(
                        f.name
                        for f in resolve_contract_fields(
                            rec.node,
                            aliases_by_rel.get(rec.file, {}),
                            by_name,
                            by_name_all=by_name_all,
                        )
                    )

            retired_upstream = {
                retired
                for ancestor in sdk_contract_ancestors(
                    class_node, aliases, by_name, by_name_all=by_name_all
                )
                for retired in sdk_retired.get(ancestor, ())
            }

            # B005: every ledger field must still exist with its recorded type
            for lf in ledger_by_contract.get(class_node.name, []):
                live = live_by_name.get(lf.field)
                if live is None and lf.field in present_names:
                    continue  # ambiguous name — the field lives on a sibling
                if live is None and lf.status == "sunset":
                    # The rule's own message names 'sunset' as the remedy for a
                    # retired field, but the status was never read — so marking
                    # it did nothing and the finding outlived the retirement.
                    # A sunset field is withdrawn by decision; 'deprecated'
                    # still means shipped-but-discouraged and must stay present.
                    continue
                if live is None and (lf.field, lf.type) in retired_upstream:
                    continue
                if live is None:
                    findings.append(
                        make_finding(
                            filename=rel,
                            rule_id="B005",
                            node=class_node,
                            message=(
                                f"Contract field '{class_node.name}.{lf.field}' "
                                f"(ledger type: '{lf.type}', status: '{lf.status}') "
                                "was removed from the contract. Entrypoint contract "
                                "fields are permanent — mark it 'deprecated' and keep "
                                "it, or mark it 'sunset' to retire it. An unmarked "
                                "removal breaks every consumer that already serializes "
                                "this field. "
                                "Suppress with '# conformance: ignore[B005] <reason>' "
                                "only if this contract has no deployed consumers."
                            ),
                            directives=directives,
                        )
                    )
                elif live.canonical_type != lf.type:
                    if _retype_is_compatible(
                        lf.type, live.canonical_type, inherited=live.node is None
                    ):
                        continue
                    expanded = (
                        _expand_aliases(
                            live.node.annotation, file_type_aliases.get(path, {})
                        )
                        if live.node is not None
                        else None
                    )
                    if expanded is not None and (
                        expanded == lf.type
                        or _retype_is_compatible(lf.type, expanded, inherited=False)
                    ):
                        continue
                    inherited_note = (
                        " (inherited from a base class or mixin)"
                        if live.node is None
                        else ""
                    )
                    findings.append(
                        make_finding(
                            filename=rel,
                            rule_id="B005",
                            node=live.node or class_node,
                            message=(
                                f"Contract field '{class_node.name}.{live.name}'"
                                f"{inherited_note} type changed from '{lf.type}' "
                                f"(ledger) to '{live.canonical_type}' (current). "
                                "Type changes break serialized payloads. Revert to "
                                f"'{lf.type}', or deprecate/sunset this field and add "
                                "a new one with the new type. "
                                "Suppress with '# conformance: ignore[B005] <reason>' "
                                "only if this contract has no deployed consumers."
                            ),
                            directives=directives,
                        )
                    )

            # B006: every live field must be recorded in the ledger
            for fi in live_fields:
                if (class_node.name, fi.name) not in ledger_by_key:
                    # An inherited field is a *new* commitment for THIS contract
                    # even when its declaring base is ledgered under its own
                    # name: the ledger records each entrypoint contract's own
                    # wire surface, and it is this contract's entries that B005
                    # consults if the class later changes base and drops the
                    # field. Regenerating records it — redeclaring it on the
                    # subclass is not required and only creates a drift site
                    # (FND-2605).
                    inherited_note = (
                        " (inherited from a base class or mixin)"
                        if fi.node is None
                        else ""
                    )
                    inherited_remedy = (
                        " Inheriting the field is enough: the generator records "
                        "an inherited field exactly like a declared one, so do "
                        "not redeclare it on this class to 'keep' it tracked."
                        if fi.node is None
                        else ""
                    )
                    findings.append(
                        make_finding(
                            filename=rel,
                            rule_id="B006",
                            node=fi.node or class_node,
                            message=(
                                f"Contract field '{class_node.name}.{fi.name}'"
                                f"{inherited_note} is not recorded in the contract "
                                "ledger (contract_schema.lock.json). Run "
                                f"'{regen}' (writes contract_schema.lock.json "
                                "in the repo root) and commit that file in the same PR. "
                                "Keep the version pin: it is the version that raised "
                                "this finding, and a bare 'uv run' resolves this repo's "
                                "locked conformance dev dependency, which — when it lags "
                                "the release the CI checker runs — rewrites the ledger "
                                "byte-identically and leaves the finding standing. "
                                "The generator is append-only — it "
                                f"can never launder a removal.{inherited_remedy}"
                            ),
                            directives=directives,
                        )
                    )

    findings.extend(
        _renamed_bundle_input_findings(
            file_trees,
            file_aliases,
            file_directives,
            by_name,
            by_name_all,
            ledger_by_contract,
            root,
        )
    )
    return findings


_LEGACY_BUNDLE_INPUT = "AppInputContract"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _renamed_bundle_inputs(
    file_trees: dict[Path, ast.AST],
) -> list[tuple[Path, ast.ClassDef]]:
    """Classes a module rebinds ``AppInputContract`` to (``AppInputContract = X``)."""
    renamed: list[tuple[Path, ast.ClassDef]] = []
    for path, tree in file_trees.items():
        if not isinstance(tree, ast.Module):
            continue
        classes = {s.name: s for s in tree.body if isinstance(s, ast.ClassDef)}
        for stmt in tree.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == _LEGACY_BUNDLE_INPUT
                and isinstance(stmt.value, ast.Name)
                and stmt.value.id in classes
            ):
                renamed.append((path, classes[stmt.value.id]))
    return sorted(renamed, key=lambda item: (str(item[0]), item[1].lineno))


def _terminal_name(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _legacy_row_anchor(
    file_trees: dict[Path, ast.AST],
    file_aliases: dict[Path, dict[str, str]],
    names: set[str],
    root: Path,
) -> tuple[Path, ast.AST] | None:
    """The first non-generated class or method that uses one of *names*.

    A finding there can carry a suppression that survives regeneration; one in
    ``app/generated/`` is overwritten by the next ``pkl eval``.
    """
    hits: list[tuple[str, int, Path, ast.AST]] = []
    for path, tree in file_trees.items():
        rel = _rel(path, root)
        if "/generated/" in f"/{rel}":
            continue
        aliases = file_aliases.get(path, {})

        def uses(node: ast.expr | None) -> bool:
            name = _terminal_name(node)
            return name is not None and aliases.get(name, name) in names

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(uses(b) for b in node.bases):
                hits.append((rel, node.lineno, path, node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = [a for a in node.args.args if a.arg not in ("self", "cls")]
                if params and uses(params[0].annotation):
                    hits.append((rel, node.lineno, path, node))
    if not hits:
        return None
    _, _, path, node = min(hits, key=lambda h: (h[0], h[1]))
    return path, node


def _renamed_bundle_input_findings(
    file_trees: dict[Path, ast.AST],
    file_aliases: dict[Path, dict[str, str]],
    file_directives: dict[Path, dict[int, _IgnoreDirective]],
    by_name: dict[str, ClassRecord],
    by_name_all: dict[str, list[ClassRecord]],
    ledger_by_contract: dict[str, list[ContractField]],
    root: Path,
) -> list[Finding]:
    """B005 for ledger rows recorded under the pre-rename bundle input name.

    Bundle ``_input.py`` modules once all declared ``class AppInputContract``;
    they now declare ``<Entrypoint>AppInputContract`` and rebind
    ``AppInputContract`` to it. The rows the ledger holds under the old name
    were recorded across every entrypoint's class, so they are checked against
    the renamed classes and any class still named ``AppInputContract``
    together: a row is removed when none of them has the field, and retyped
    when every one that has it changed its type. A field on a class still named
    ``AppInputContract`` is left to the main pass, which checks that class.
    """
    rows = ledger_by_contract.get(_LEGACY_BUNDLE_INPUT)
    renamed = _renamed_bundle_inputs(file_trees)
    if not rows or not renamed:
        return []
    live: dict[str, list[tuple[str, bool]]] = {}
    for path, node in renamed:
        for f in resolve_contract_fields(
            node, file_aliases.get(path, {}), by_name, by_name_all=by_name_all
        ):
            live.setdefault(f.name, []).append((f.canonical_type, f.node is None))
    still_named: set[str] = set()
    real = [
        rec
        for rec in by_name_all.get(_LEGACY_BUNDLE_INPUT, [])
        if isinstance(rec.node, ast.ClassDef)
    ]
    for rec in real:
        still_named.update(
            f.name
            for f in resolve_contract_fields(
                rec.node,
                file_aliases.get(Path(root, rec.file), {}),
                by_name,
                by_name_all=by_name_all,
            )
        )
    names = {_LEGACY_BUNDLE_INPUT, *(node.name for _, node in renamed)}
    anchor = _legacy_row_anchor(file_trees, file_aliases, names, root) or renamed[0]
    anchor_path, anchor_node = anchor
    listed = ", ".join(sorted(names - {_LEGACY_BUNDLE_INPUT}))
    findings: list[Finding] = []
    for lf in rows:
        types = live.get(lf.field)
        if lf.status == "sunset" or lf.field in still_named or (real and not types):
            continue
        if types and any(
            t == lf.type or _retype_is_compatible(lf.type, t, inherited=inherited)
            for t, inherited in types
        ):
            continue
        if types:
            change = (
                f"changed type from '{lf.type}' (ledger) to "
                f"{', '.join(sorted({repr(t) for t, _ in types}))} (current). "
                "Type changes break serialized payloads. Revert the type, or "
                "deprecate/sunset this field and add a new one with the new type. "
            )
        else:
            change = (
                f"(ledger type: '{lf.type}', status: '{lf.status}') was removed "
                "from the contract. Entrypoint contract fields are permanent — "
                "mark it 'deprecated' and keep it, or mark it 'sunset' to retire "
                "it. "
            )
        findings.append(
            make_finding(
                filename=_rel(anchor_path, root),
                rule_id="B005",
                node=anchor_node,
                message=(
                    f"Contract field '{_LEGACY_BUNDLE_INPUT}.{lf.field}' {change}"
                    f"The ledger recorded it under '{_LEGACY_BUNDLE_INPUT}' before "
                    "contract-toolkit named bundle input classes per entrypoint; "
                    f"it is checked against {listed}. "
                    "Suppress with '# conformance: ignore[B005] <reason>' "
                    "only if this contract has no deployed consumers."
                ),
                directives=file_directives.get(anchor_path, {}),
            )
        )
    return findings
