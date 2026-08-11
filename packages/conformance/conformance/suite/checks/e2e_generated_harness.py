"""T023–T024 — the e2e harness scaffold comes from contract/app.pkl, not by hand.

``contract/app.pkl`` is the single source of truth for a connector's identity,
and the contract toolkit already emits the whole e2e scaffold from it
(``pkl eval -m . contract/app.pkl``):

* ``app/generated/_e2e_base.py`` — ``<Name>GeneratedE2EBase``, carrying
  ``connector_short_name``, ``argo_package_name``, ``argo_template_name``,
  ``app_service_url``, ``connection_type`` and ``connection_category``, and
  already parented to ``SQLAppE2ETest`` or ``BaseE2ETest`` according to the
  declared connector category.
* ``app/generated/_e2e_credential.py`` — ``<Name>CredentialBody`` (direct) and
  ``<Name>AgentCredentialBody`` (the lightweight SDR body), typed from the
  contract's ``credentialCommonFields`` / ``credentialAuthOptions``.
* ``app/generated/_e2e_substitutions.py`` — ``<Name>MustacheSubstitutions``,
  typed from the contract's ``uiConfig`` inputs (including the
  ``Literal["direct", "agent"]`` widening that lets an AGENT-mode run submit
  ``extraction_method="agent"``).

A connector's e2e test is then a thin subclass that supplies only what the
contract cannot know: the source under test, the asset floors, and the run
mode.  ``atlan-mysql-app``'s ``tests/e2e/test_mysql_full_dag.py`` is the
reference shape.

Hand-writing any of that scaffold inside ``tests/`` re-derives generated truth
into a file no generator owns.  It looks correct in review and is **not**
reverted by the next ``poe generate`` — it simply stops agreeing with the
contract, silently, the moment the contract moves.  A renamed Argo template, a
changed ``app_service_url``, a new auth option, a connector whose
``connection_type`` differs from its app name: each drifts only in the
hand-written copy, and the failure surfaces as a tenant-side AE error in a
120-minute e2e run rather than a diff.  This is the same defect class as the
hand-edited ``{{agent-json}}`` manifest slot that K003–K010 police on the
generated-artifact side — here the artifact is correct and the test declines to
use it.

* ``T023`` — E2EHarnessScaffoldHandWritten: a module under ``tests/`` declares
  scaffold the toolkit generates — identity attrs on an e2e harness subclass, a
  ``CredentialBody`` subclass, or a ``MustacheSubstitutions`` /
  ``SQLMustacheSubstitutions`` subclass.  Import the generated module instead.

* ``T024`` — E2ERunModeUnset: a collectable e2e test class never declares
  ``mode``.  ``BaseE2ETest.mode`` defaults to ``RunMode.DIRECT``, but the
  reusable Tests workflow's e2e job always brings up a **CI-side** worker
  container on a per-leg Temporal queue and expects the AE extract activity to
  be routed to it — that routing only happens under ``RunMode.AGENT``.  Left at
  the default, the harness dispatches extraction to the tenant's own production
  queue: the container under test never runs, and the run either hangs on a
  queue no CI worker polls or greens against code that was never exercised.
  Agent mode is also the self-deployed-runtime path itself, which is why
  ``T002`` accepts ``mode = RunMode.AGENT`` as SDR coverage.  Declare the mode
  explicitly — ``RunMode.AGENT`` for the normal CI-worker run, or
  ``RunMode.DIRECT`` deliberately for a tier-5 run against a deployed tenant
  pod.

Discovery
---------
Walks the whole ``tests/`` tree (a shared harness base may live outside
``tests/e2e/``) and additionally indexes ``**/generated/**/_e2e_base.py`` so a
subclass of a generated base is recognised as an e2e harness class.  Inheritance
is resolved transitively across repo-local classes, so a connector that funnels
its suites through an in-repo base is graded on that base, not on each leaf.

The class index is keyed by ``(file, class name)`` and the visited class is
always graded from the node in hand; a bare name resolves an ancestor only, and
prefers a definition in the referencing file.  ``from X import Y as Z`` bindings
are recorded per file and resolved first, so an aliased import of a generated
base (``from app.generated._e2e_base import FooGeneratedE2EBase as Base``)
reaches the real class rather than falling through to the bare-name lookup and
grading the subclass as a non-harness.  When a bare name maps to classes in
several other files the resolution is ambiguous, and the checker biases toward
reporting **provided some candidate transitively reaches an SDK harness base** —
a suppression carrying the author's reason is auditable, silence is not, but an
ordinary class that merely shares a name with an unrelated one is not this
rule's business and must not be graded as a harness.

Known limit: alias bindings record only ``{spelled: imported}``, not the module
the import came from, so a name that is defined in several indexed files cannot
be disambiguated to the module-qualified one, and re-export chains (an
intermediate module that does ``from generated import Foo as Bar``) are not
followed.  Neither shape occurs in the real consumer layout (a direct aliased
import of a generated base, which IS resolved); if one ever appears, record the
``ImportFrom`` module and resolve module-qualified candidates first.

Inline suppression
------------------
``# conformance: ignore[T023] <reason>`` / ``ignore[T024]`` on the flagged line
or the comment-only line directly above it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    is_collectable_test_file,
    is_test_class,
    make_cli_main,
    make_finding,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T023 = "T023"
RULE_T024 = "T024"

# Harness bases the SDK exports. A repo class reaching either of these
# (directly, via a generated base, or via an in-repo intermediate) is an e2e
# harness class.
_SDK_HARNESS_BASES: frozenset[str] = frozenset({"BaseE2ETest", "SQLAppE2ETest"})

# Attributes the toolkit emits into <Name>GeneratedE2EBase. Re-declaring any of
# them in tests/ forks the contract.
_GENERATED_IDENTITY_ATTRS: tuple[str, ...] = (
    "connector_short_name",
    "argo_package_name",
    "argo_template_name",
    "app_service_url",
    "connection_type",
    "connection_category",
)

# Model bases the toolkit generates concrete subclasses of.
_GENERATED_MODEL_BASES: dict[str, str] = {
    "CredentialBody": "app/generated/_e2e_credential.py "
    "(<Name>CredentialBody / <Name>AgentCredentialBody)",
    "MustacheSubstitutions": "app/generated/_e2e_substitutions.py "
    "(<Name>MustacheSubstitutions)",
    "SQLMustacheSubstitutions": "app/generated/_e2e_substitutions.py "
    "(<Name>MustacheSubstitutions)",
}

_MODE_ATTR = "mode"
_GENERATED_BASE_FILENAME = "_e2e_base.py"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def discover(root: Path) -> list[Path]:
    """Walk ``tests/`` for all Python source files."""
    base = root / "tests"
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


# ---------------------------------------------------------------------------
# Class indexing
# ---------------------------------------------------------------------------


def _base_names(node: ast.ClassDef) -> set[str]:
    """Bare names of *node*'s bases (``X`` and ``mod.X`` both yield ``X``)."""
    names: set[str] = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return names


def _class_body_assignments(node: ast.ClassDef) -> dict[str, ast.stmt]:
    """Direct class-body attribute assignments, ``{attr: statement}``.

    Only statements in the class body itself — not nested functions or nested
    classes — so a local variable named ``mode`` inside a method never counts.
    """
    out: dict[str, ast.stmt] = {}
    for item in node.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, item)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if item.value is not None:
                out.setdefault(item.target.id, item)
    return out


class _ClassInfo:
    """One indexed class: where it lives, what it inherits, what it declares."""

    __slots__ = ("assignments", "bases", "file", "node")

    def __init__(self, file: str, node: ast.ClassDef) -> None:
        self.file = file
        self.node = node
        self.bases = _base_names(node)
        self.assignments = _class_body_assignments(node)


def _generated_base_files(root: Path) -> list[Path]:
    """Locate committed ``_e2e_base.py`` modules the toolkit emitted.

    Both the single-entrypoint (``app/generated/_e2e_base.py``) and the
    multi-entrypoint (``app/generated/<entrypoint>/_e2e_base.py``) layouts are
    covered, as is the ``contract/generated/`` variant some connectors emit to.
    """
    out: list[Path] = []
    for parent in ("app", "contract"):
        base = root / parent / "generated"
        if not base.is_dir():
            continue
        out.extend(
            p
            for p in base.rglob(_GENERATED_BASE_FILENAME)
            if p.is_file() and "__pycache__" not in p.parts
        )
    return sorted(out)


#: Class index keyed by ``(file, class name)``.  A bare-name key would let an
#: unrelated same-named class in another file (``TestBase``, ``TestFullDag`` and
#: friends recur across a connector's test tree) shadow the real one and
#: silently void both rules — evidence must come from the same scope as the
#: unit being graded.
_ClassIndex = dict[tuple[str, str], _ClassInfo]
#: Secondary bare-name map, used ONLY to resolve an ancestor defined in another
#: file (the intentional transitive-base case, e.g. a generated ``_e2e_base``).
_NameIndex = dict[str, list[_ClassInfo]]
#: Per-file ``from X import Y as Z`` bindings, ``{file: {spelled: imported}}``.
#: Recorded at index time so ``class T(Base):`` where ``Base`` is an aliased
#: import of a generated base resolves to the real class — matching the
#: referent, not the spelling.  Plain (non-aliased) imports are excluded: their
#: binding name IS the imported name, which the bare-name fallback already
#: resolves.
_AliasIndex = dict[str, dict[str, str]]


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """``from X import Y as Z`` bindings of a module, ``{spelled: imported}``."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname is not None:
                    out[alias.asname] = alias.name
    return out


def _index_file(
    path: Path,
    rel: str,
    index: _ClassIndex,
    by_name: _NameIndex,
    aliases: _AliasIndex | None = None,
) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return
    if aliases is not None:
        aliases[rel] = _import_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            info = _ClassInfo(rel, node)
            index.setdefault((rel, node.name), info)
            by_name.setdefault(node.name, []).append(info)


def _resolve_base(
    name: str,
    from_file: str,
    index: _ClassIndex,
    by_name: _NameIndex,
    aliases: _AliasIndex | None = None,
) -> tuple[_ClassInfo | None, bool]:
    """Resolve base class *name* as referenced from *from_file*.

    Returns ``(info, ambiguous)``.  A definition in the referencing file always
    wins; then a ``from X import Y as name`` binding resolves *name* to the
    imported ``Y`` (the referent, not the spelling); otherwise the bare name is
    resolved cross-file.  When the name maps to definitions in more than one
    other file, static reach has run out and ``ambiguous`` is True — callers
    then bias toward reporting (a suppression carrying the author's reason is
    auditable; silence is not).
    """
    same_file = index.get((from_file, name))
    if same_file is not None:
        return same_file, False
    if aliases is not None:
        imported = aliases.get(from_file, {}).get(name)
        if imported is not None and imported != name:
            candidates = by_name.get(imported, [])
            if len(candidates) == 1:
                return candidates[0], False
    candidates = by_name.get(name, [])
    if not candidates:
        return None, False
    if len({c.file for c in candidates}) > 1:
        # Report-on-ambiguity needs a floor, or ordinary classes that merely
        # share a base name with something elsewhere in tests/ get graded as
        # harnesses. Raise the flag only when some candidate could actually BE
        # a harness — reaching an SDK harness base at ANY depth, not just
        # directly, since routing suites through an in-repo intermediate base is
        # the supported shape. A direct-bases-only floor both missed those and
        # made the verdict depend on which candidate sorted first.
        if any(_reaches_harness_base(c, index, by_name, aliases) for c in candidates):
            return candidates[0], True
        return candidates[0], False
    return candidates[0], False


def _reaches_harness_base(
    info: _ClassInfo,
    index: _ClassIndex,
    by_name: _NameIndex,
    aliases: _AliasIndex | None = None,
) -> bool:
    """Whether *info* transitively reaches an SDK harness base.

    Used only as the ambiguity floor, so it resolves conservatively (a bare name
    with several definitions contributes every one of them) and never re-enters
    :func:`_resolve_base`, which would recurse back into the floor.
    """
    seen: set[tuple[str, str]] = set()
    stack = [info]
    while stack:
        current = stack.pop()
        key = (current.file, current.node.name)
        if key in seen:
            continue
        seen.add(key)
        if current.bases & _SDK_HARNESS_BASES:
            return True
        for base in current.bases:
            same = index.get((current.file, base))
            if same is not None:
                stack.append(same)
                continue
            if aliases is not None:
                imported = aliases.get(current.file, {}).get(base)
                if imported is not None and imported != base:
                    stack.extend(by_name.get(imported, []))
                    continue
            stack.extend(by_name.get(base, []))
    return False


def _is_harness_class(
    info: _ClassInfo,
    index: _ClassIndex,
    by_name: _NameIndex,
    aliases: _AliasIndex | None = None,
) -> bool:
    """True when *info*'s class transitively inherits an SDK e2e harness base."""
    seen: set[tuple[str, str]] = set()

    def walk(current: _ClassInfo) -> bool:
        key = (current.file, current.node.name)
        if key in seen:
            return False
        seen.add(key)
        if current.bases & _SDK_HARNESS_BASES:
            return True
        for base in current.bases:
            resolved, ambiguous = _resolve_base(
                base, current.file, index, by_name, aliases
            )
            if ambiguous:
                return True  # cannot rule the harness out — grade it
            if resolved is not None and walk(resolved):
                return True
        return False

    return walk(info)


def _generated_model_base_reached(
    info: _ClassInfo,
    index: _ClassIndex,
    by_name: _NameIndex,
    aliases: _AliasIndex | None = None,
) -> str | None:
    """The generated-model base *info*'s class transitively inherits, if any.

    The hand-written model the rule exists to catch hides behind one hop of
    indirection as easily as it declares the base directly — an intermediate
    ``class BaseCredential(CredentialBody)`` defined elsewhere in the repo, or
    an aliased import (``from ... import CredentialBody as Body``) — so
    matching direct base names alone under-reports exactly the shape the rule
    targets.  This walks the same alias-aware transitive resolver the harness
    branches use and returns the matched model base for the message.

    Unlike the harness walk, ambiguity does not force a verdict: an ambiguous
    model-base name is an ordinary class that merely shares a name with a
    generated model, not this rule's business — and ``CredentialBody`` /
    ``MustacheSubstitutions`` are SDK classes no repo index can disambiguate,
    so the walk simply continues through resolvable bases.
    """
    seen: set[tuple[str, str]] = set()

    def walk(current: _ClassInfo) -> str | None:
        key = (current.file, current.node.name)
        if key in seen:
            return None
        seen.add(key)
        direct = current.bases & _GENERATED_MODEL_BASES.keys()
        if direct:
            return sorted(direct)[0]
        for base in current.bases:
            # An aliased import of a generated model base
            # (``from ... import CredentialBody as Body``) never enters the
            # class index — CredentialBody is an SDK class — so resolve the
            # spelling to its referent before walking the index.
            if aliases is not None:
                imported = aliases.get(current.file, {}).get(base)
                if imported in _GENERATED_MODEL_BASES:
                    return imported
            resolved, ambiguous = _resolve_base(
                base, current.file, index, by_name, aliases
            )
            if ambiguous or resolved is None:
                continue
            reached = walk(resolved)
            if reached is not None:
                return reached
        return None

    return walk(info)


def _declares_mode(
    info: _ClassInfo,
    index: _ClassIndex,
    by_name: _NameIndex,
    aliases: _AliasIndex | None = None,
) -> bool:
    """True when the class or a repo-visible ancestor sets a class-level ``mode``."""
    seen: set[tuple[str, str]] = set()

    def walk(current: _ClassInfo) -> bool:
        key = (current.file, current.node.name)
        if key in seen:
            return False
        seen.add(key)
        if _MODE_ATTR in current.assignments:
            return True
        for base in current.bases:
            resolved, ambiguous = _resolve_base(
                base, current.file, index, by_name, aliases
            )
            if ambiguous:
                # `ambiguous` answers _is_harness_class's question ("could any
                # candidate make this a harness"), not this one. Blanket-skipping
                # made the floor grade the class and then deny it the ancestor
                # that would clear it. Consult the candidates instead: if EVERY
                # harness-reaching candidate declares `mode`, that is knowable.
                harness_candidates = [
                    c
                    for c in by_name.get(base, [])
                    if _reaches_harness_base(c, index, by_name, aliases)
                ]
                if harness_candidates and all(walk(c) for c in harness_candidates):
                    return True
                continue
            if resolved is not None and walk(resolved):
                return True
        return False

    return walk(info)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _t023_identity_message(class_name: str, attrs: list[str]) -> str:
    listed = ", ".join(attrs)
    return (
        f"class {class_name!r} hand-declares e2e identity attrs the contract toolkit "
        f"already generates from contract/app.pkl: {listed}. The generated "
        "app/generated/_e2e_base.py defines <Name>GeneratedE2EBase with exactly these "
        "values, parented to SQLAppE2ETest or BaseE2ETest per the declared connector "
        "category. A hand-written copy is owned by no generator, so it stops agreeing "
        "with the contract the moment the contract moves (renamed Argo template, "
        "changed service URL, a connection_type that differs from the app name) — and "
        "the drift only surfaces as a tenant-side AE failure in a 120-minute e2e run. "
        "Subclass the generated base instead and keep only the connector-specific "
        "knobs (mode, filters, database_spec(), asset floors): "
        "`from app.generated._e2e_base import <Name>GeneratedE2EBase`. If the module "
        "is absent, regenerate with `pkl eval -m . contract/app.pkl` (see K010) — "
        "never hand-edit generated output."
    )


def _t023_model_message(class_name: str, base: str) -> str:
    return (
        f"class {class_name!r} hand-writes a {base} subclass under tests/. The "
        f"contract toolkit generates this model from contract/app.pkl into "
        f"{_GENERATED_MODEL_BASES[base]}, typed from the contract's own credential "
        "and uiConfig declarations. A hand-written model duplicates the field set, "
        "the aliases and the auth defaults with nothing keeping them in sync — a new "
        "auth option or a re-typed input lands in the generated module and never in "
        "the test, and the AE rejects (or worse, silently mis-executes) the payload. "
        "Import the generated class instead; regenerate with "
        "`pkl eval -m . contract/app.pkl` if the module is absent. When the contract "
        "genuinely cannot express a field, fix it at the pkl source rather than "
        "re-typing the model here."
    )


def _t024_message(class_name: str) -> str:
    return (
        f"e2e test class {class_name!r} never declares `mode`, so it inherits "
        "BaseE2ETest's default RunMode.DIRECT. The reusable Tests workflow's e2e job "
        "always starts a CI-side worker container on a per-leg Temporal queue and "
        "expects the AE extract activity to be routed to it — routing that only "
        "happens under RunMode.AGENT. Left at the default the harness dispatches "
        "extraction to the tenant's own production queue, so the container under test "
        "never runs: the run hangs on a queue no CI worker polls, or greens against "
        "code it never exercised. Agent mode is also the self-deployed-runtime path "
        "T002 requires SDR apps to cover. Declare it explicitly: "
        "`mode = RunMode.AGENT` (the normal CI-worker run), or `RunMode.DIRECT` when "
        "a tier-5 run against a deployed tenant pod is genuinely intended."
    )


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: T023/T024 resolve inheritance across files; use scan_all."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Grade the repo's e2e harness classes (T023, T024)."""
    index: _ClassIndex = {}
    by_name: _NameIndex = {}
    aliases: _AliasIndex = {}

    # Generated bases first: a test subclassing one must resolve to a harness
    # class even though the module lives outside tests/.
    for gen in _generated_base_files(root):
        try:
            rel_gen = str(gen.relative_to(root))
        except ValueError:
            rel_gen = str(gen)
        _index_file(gen, rel_gen, index, by_name, aliases)

    test_files: list[tuple[Path, str, str]] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        test_files.append((path, rel, text))
        _index_file(path, rel, index, by_name, aliases)

    findings: list[Finding] = []
    for _path, rel, text in test_files:
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError:
            continue
        directives = _parse_directives(text)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            # Grade the node in hand — never route the visited class through a
            # bare-name lookup that could resolve to a different file's class.
            info = _ClassInfo(rel, node)
            assignments = info.assignments

            # T023(b/c) — a hand-written generated-model subclass, resolved
            # transitively and alias-aware so an intermediate repo base or an
            # aliased import of the generated model cannot hide the match.
            model_base = _generated_model_base_reached(info, index, by_name, aliases)
            if model_base is not None:
                findings.append(
                    make_finding(
                        filename=rel,
                        rule_id=RULE_T023,
                        node=node,
                        message=_t023_model_message(node.name, model_base),
                        directives=directives,
                    )
                )

            if not _is_harness_class(info, index, by_name, aliases):
                continue

            # T023(a) — identity attrs re-declared on a harness subclass.
            declared = [a for a in _GENERATED_IDENTITY_ATTRS if a in assignments]
            if declared:
                findings.append(
                    make_finding(
                        filename=rel,
                        rule_id=RULE_T023,
                        node=assignments[declared[0]],
                        message=_t023_identity_message(node.name, declared),
                        directives=directives,
                    )
                )

            # T024 — a collectable e2e test class with no declared run mode.
            # The file must be one pytest actually collects: a shared harness
            # base in tests/e2e/helpers.py only matters through a leaf subclass
            # that IS collected, and grading it there is a false positive.
            if (
                is_collectable_test_file(Path(rel).name)
                and is_test_class(node)
                and not _declares_mode(info, index, by_name, aliases)
            ):
                findings.append(
                    make_finding(
                        filename=rel,
                        rule_id=RULE_T024,
                        node=node,
                        message=_t024_message(node.name),
                        directives=directives,
                    )
                )

    return findings


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=(
        "T023/T024: e2e harness scaffold must come from the pkl-generated modules, "
        "and e2e test classes must declare their RunMode."
    ),
    default_scan_paths=("tests",),
)
"""CLI entry point for the T023/T024 e2e generated-harness checks."""


if __name__ == "__main__":
    sys.exit(main())
