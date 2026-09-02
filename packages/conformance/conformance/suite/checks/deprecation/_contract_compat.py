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
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    make_finding,
)
from conformance.suite.checks._entrypoint_contract_fields import (
    collect_entrypoint_contract_names,
    resolve_contract_fields,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
)
from conformance.suite.schema.disposition import RuleScope
from conformance.suite.schema.findings import Finding

from ._ledger_schema import ContractField, ContractLedger, regen_command

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


def scan_contract_compat(
    paths: list[Path],
    root: Path,
    ledger: ContractLedger,
    scope: RuleScope | None = None,
) -> list[Finding]:
    """Emit B005/B006 for entrypoint contract backwards-compatibility violations.

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
    by_name: dict[str, ClassRecord] = {}
    # Every declaration per class name, not just the first. The ledger keys
    # fields by BARE class name, so a name declared in two modules makes the
    # ledger ambiguous — see the B005 presence check below.
    by_name_all: dict[str, list[ClassRecord]] = {}
    aliases_by_rel: dict[str, dict[str, str]] = {}

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
        aliases_by_rel[rel] = aliases
        for rec in collect_classes(tree, rel, aliases):
            by_name.setdefault(rec.name, rec)
            by_name_all.setdefault(rec.name, []).append(rec)

    entrypoint_names = collect_entrypoint_contract_names(file_trees, by_name)

    if not entrypoint_names:
        return []

    # Pre-index the ledger for O(1) lookups
    ledger_by_key: dict[tuple[str, str], ContractField] = {
        (f.contract, f.field): f for f in ledger.fields
    }
    ledger_by_contract: dict[str, list[ContractField]] = {}
    for f in ledger.fields:
        ledger_by_contract.setdefault(f.contract, []).append(f)

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
                    inherited_note = (
                        " (inherited from a base class or mixin)"
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
                                "can never launder a removal."
                            ),
                            directives=directives,
                        )
                    )

    return findings
