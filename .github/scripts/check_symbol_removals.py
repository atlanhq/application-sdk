#!/usr/bin/env python3
"""Importable-surface removal gate (FND-2388).

Answers the one question no other check in this repo asks: **what did this
change take away?**

Every existing surface guard compares the tree against itself.
``capability-manifest-check`` regenerates ``docs/agents/sdk-capabilities.md``
and diffs it against the committed copy, so it proves the manifest is fresh —
not that the surface is intact.  The deprecated-symbol manifest
(``gen-deprecations``) scans for *live* markers, so a symbol deleted outright
leaves nothing behind for it to record.  Neither can see a deletion, and that
is exactly how 3.36.0 shipped nine removed ``preflight_gate`` names and walled
off fifteen connector repos at test collection.

This gate compares the surface at ``HEAD`` against the surface at the **last
released tag** — the thing consumers actually pinned — and fails when a name
that shipped has simply stopped existing.

The policy, in one line
-----------------------

**You may not remove a name that was not already marked deprecated in the
previous release.**

Converting a name into a deprecated alias keeps it *present*, so a properly
deprecated removal produces no finding at all: there is nothing to except, no
second registry to keep in sync, and no way for the excuse to drift away from
the code.  ``@deprecated`` on a def or class, or a PEP 562 ``__getattr__``
alias entry, both keep the name in the snapshot.  When the deprecation horizon
finally arrives, the name is deleted from a base that already marked it
deprecated, and the gate stays quiet.

Two severities, and why the split is where it is
------------------------------------------------

``blocking`` — the name is public, or it is a public name living in a private
module (``application_sdk.execution._temporal.preflight_gate.resolve_gate_attempts``).
This is five of the nine names FND-2388 lost.  A public name in a private
module reads as callable API to everybody who finds it, and the SDK has no
mechanism that stops an app importing it.

``advisory`` — the name itself is underscore-prefixed
(``_GATE_BROKEN_CATEGORIES``, ``_is_gate_broken``).  Reported, never fails.
Renaming a private helper is ordinary refactoring and making it cost a
deprecation cycle would be a tax on every internal change.  The fleet's use of
these four names is a *consumer-side* defect, and conformance rule B008
``PrivateSdkModuleImport`` is the fix for it — not a permanent freeze on the
SDK's internals.

A blocking removal has exactly two ways through: deprecate it first (the
supported path, see ``docs/standards/symbols.md``), or declare the break
in the commit subject (``feat!:`` / ``fix!:`` / a ``BREAKING CHANGE:`` trailer),
which routes it to a major bump in ``release-version-bump.yaml`` instead of
riding out on a patch the way #3685 did.

What this gate does not cover
-----------------------------

It guards the **shape** of the surface, not its semantics. Specifically, it does
not compare default *values*: changing ``mode: PreflightGateMode =
PreflightGateMode.SOFT`` to ``mode: PreflightGateMode | None = None`` produces no
finding, because the parameter still exists and still has a default.

That is deliberate. The SDK changes defaults often and on purpose, and a gate
that argued about every one would be noise nobody reads. But it is a genuine
blind spot rather than covered ground: a default-value change can alter
behaviour for every caller who never passed the argument, and nothing here will
say so. Do not read a green run as "the behaviour is unchanged" — it means no
name vanished and no signature narrowed.

Also not covered: a changed return type, a narrowed exception contract, and any
behavioural change behind an unchanged signature.

Why AST and not import
----------------------

The base side is a checkout of a *previous release*, whose dependency set no
longer matches the current lock.  Importing it would mean resolving an old
environment in CI, and an import failure would silently degrade the gate to
"no removals found" — the fail-open shape that makes a gate worthless.  Parsing
is hermetic: a file that will not parse is reported, never skipped silently.

Exit codes:
  * 0 — no blocking removals (advisory findings may still be reported).
  * 1 — at least one blocking removal with no deprecation and no declared break.
  * 2 — the gate could not run (bad arguments, unreadable tree, unparseable
        module).  Never confused with "clean": a gate that cannot see must not
        report success.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

#: The distribution package whose surface this gate guards.
DEFAULT_PACKAGE = "application_sdk"

#: Module-level dict literal that maps a PEP 562 alias name to its replacement.
#: Prescribed by ``docs/standards/symbols.md`` so this gate can read the alias
#: set statically; ``application_sdk/execution/_temporal/preflight_gate.py`` is
#: the reference implementation.
ALIAS_MAPPING_NAME = "_DEPRECATED_CONSTANTS"

#: Decorator names that mark a def/class as deprecated (``typing_extensions``
#: and ``warnings`` both export ``deprecated``; either import style is matched
#: on the attribute name alone).
_DEPRECATED_DECORATORS = frozenset({"deprecated"})

#: Conventional-commit shapes that declare a break. ``!`` before the colon, or a
#: ``BREAKING CHANGE``/``BREAKING-CHANGE`` trailer anywhere in the body.
_BREAKING_SUBJECT = re.compile(r"^[a-zA-Z]+(\([^)]*\))?!:")
_BREAKING_TRAILER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)

#: Dunder names that are module plumbing rather than surface. ``__getattr__``
#: and ``__all__`` describe the surface; they are not themselves part of it.
_PLUMBING = frozenset(
    {
        "__all__",
        "__getattr__",
        "__dir__",
        "__doc__",
        "__future__",
        "__name__",
        "__path__",
        "__version__",
        "annotations",
    }
)


# ---------------------------------------------------------------------------
# Snapshot model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    """One importable name, keyed in a snapshot by ``module:qualname``."""

    module: str
    qualname: str
    kind: str
    """``function`` | ``class`` | ``method`` | ``constant`` | ``reexport`` | ``alias``."""
    deprecated: bool
    """True when the name carries ``@deprecated`` or is served by a PEP 562 alias."""
    params: tuple[str, ...] = ()
    """Parameter names for a callable, in declaration order."""
    required_params: tuple[str, ...] = ()
    """The subset of *params* with no default — narrowing these is a break."""

    @property
    def key(self) -> str:
        return f"{self.module}:{self.qualname}"

    @property
    def is_private_name(self) -> bool:
        """True when any part of the qualname is underscore-prefixed (advisory tier).

        Every component counts, so a public method on a private class
        (``_Helper.run``) is as advisory as the class itself — the owner is
        private, so nothing hanging off it was ever offered.

        Dunders are not private in this sense: ``__init__`` narrowing is a real
        break for anyone subclassing, and that subclass may be an app's.
        """
        return any(
            part.startswith("_") and not part.startswith("__")
            for part in self.qualname.split(".")
        )


@dataclass
class Snapshot:
    """The importable surface of one tree."""

    package: str
    symbols: dict[str, Symbol] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "package": self.package,
            "symbols": {k: asdict(v) for k, v in sorted(self.symbols.items())},
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Snapshot:
        raw = json.loads(text)
        symbols = {
            key: Symbol(
                module=rec["module"],
                qualname=rec["qualname"],
                kind=rec["kind"],
                deprecated=rec["deprecated"],
                params=tuple(rec.get("params", ())),
                required_params=tuple(rec.get("required_params", ())),
            )
            for key, rec in raw["symbols"].items()
        }
        return cls(package=raw["package"], symbols=symbols)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def module_path(file: Path, package_root: Path, package: str) -> str:
    """Dotted module path for *file* beneath *package_root*.

    ``application_sdk/app/__init__.py`` → ``application_sdk.app``;
    ``application_sdk/discovery.py``    → ``application_sdk.discovery``.
    """
    rel = file.relative_to(package_root).with_suffix("")
    parts = [package, *rel.parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def iter_modules(package_root: Path) -> Iterator[Path]:
    """Every ``.py`` file under *package_root*, in stable order."""
    yield from sorted(p for p in package_root.rglob("*.py") if p.is_file())


def _is_deprecated_decorator(node: ast.expr) -> bool:
    """True for ``@deprecated(...)`` / ``@typing_extensions.deprecated(...)``."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id in _DEPRECATED_DECORATORS
    if isinstance(target, ast.Attribute):
        return target.attr in _DEPRECATED_DECORATORS
    return False


def _signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(all param names, required param names)`` for *node*.

    ``self``/``cls`` are dropped so a method reads the same as the call an app
    actually writes.  ``*args``/``**kwargs`` are recorded by name because their
    disappearance narrows the surface too.
    """
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    names: list[str] = [a.arg for a in positional]
    if names and names[0] in ("self", "cls"):
        names.pop(0)
        positional = positional[1:]

    # Defaults right-align against the positional list.
    n_defaulted = len(args.defaults)
    required = names[: len(names) - n_defaulted] if n_defaulted else list(names)

    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    for kwarg, default in zip(args.kwonlyargs, args.kw_defaults):
        names.append(kwarg.arg)
        if default is None:
            required.append(kwarg.arg)
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return tuple(names), tuple(required)


def _alias_names(tree: ast.Module) -> set[str]:
    """Names served by the module's PEP 562 deprecation shim.

    Reads the keys of the module-level ``_DEPRECATED_CONSTANTS`` dict literal,
    and only when the module also defines ``__getattr__`` — the dict alone
    serves nothing, and a module that has the shim without the mapping is
    serving names this gate cannot enumerate (reported by the caller).
    """
    has_getattr = any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "__getattr__"
        for n in tree.body
    )
    if not has_getattr:
        return set()
    names: set[str] = set()
    for node in tree.body:
        targets: Sequence[ast.expr]
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == ALIAS_MAPPING_NAME for t in targets
        ):
            continue
        if isinstance(node.value, ast.Dict):
            names.update(
                k.value
                for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )
    return names


def _declared_all(tree: ast.Module) -> frozenset[str] | None:
    """The module's ``__all__`` as a set, or ``None`` when it declares none."""
    for node in tree.body:
        targets: Sequence[ast.expr]
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
            return frozenset(
                e.value
                for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
    return None


def _reexported_names(
    node: ast.Import | ast.ImportFrom,
    declared_all: frozenset[str] | None,
    package: str,
) -> list[str]:
    """Names *node* re-exports as part of this module's surface.

    A top-level import always *binds* a name, but binding is not publishing.
    Two signals separate the two, and a module needs only one:

    * ``__all__`` lists the name — an explicit declaration, and the strongest
      signal there is;
    * the import is intra-package (a relative import, or an absolute one rooted
      at *package*) — the shape of a deliberate re-export, and the one every
      package ``__init__`` in this repo uses.

    Everything else — ``from decimal import Decimal`` in a leaf module — is an
    implementation detail. Counting those would fail the gate on any tidy-up of
    an unused import, which is the fastest way to teach people to ignore it.
    """
    intra_package = False
    if isinstance(node, ast.ImportFrom):
        intra_package = bool(node.level) or (
            node.module is not None
            and (node.module == package or node.module.startswith(package + "."))
        )
    else:
        intra_package = any(
            alias.name == package or alias.name.startswith(package + ".")
            for alias in node.names
        )

    names: list[str] = []
    for alias in node.names:
        if alias.name == "*":
            continue
        bound = alias.asname or alias.name.split(".")[0]
        if declared_all is not None:
            if bound in declared_all:
                names.append(bound)
            continue
        if intra_package:
            names.append(bound)
    return names


def extract_module(
    tree: ast.Module, module: str, package: str = DEFAULT_PACKAGE
) -> list[Symbol]:
    """Every importable name bound at the top level of *tree*."""
    aliases = _alias_names(tree)
    declared_all = _declared_all(tree)
    symbols: list[Symbol] = []

    def add(
        qualname: str, kind: str, *, deprecated: bool = False, sig=((), ())
    ) -> None:
        if qualname in _PLUMBING:
            return
        symbols.append(
            Symbol(
                module=module,
                qualname=qualname,
                kind=kind,
                deprecated=deprecated or qualname in aliases,
                params=sig[0],
                required_params=sig[1],
            )
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(
                node.name,
                "function",
                deprecated=any(
                    _is_deprecated_decorator(d) for d in node.decorator_list
                ),
                sig=_signature(node),
            )
        elif isinstance(node, ast.ClassDef):
            cls_deprecated = any(
                _is_deprecated_decorator(d) for d in node.decorator_list
            )
            add(node.name, "class", deprecated=cls_deprecated)
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if child.name.startswith("_") and child.name != "__init__":
                    continue
                add(
                    f"{node.name}.{child.name}",
                    "method",
                    deprecated=cls_deprecated
                    or any(_is_deprecated_decorator(d) for d in child.decorator_list),
                    sig=_signature(child),
                )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    add(target.id, "constant")
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                add(node.target.id, "constant")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # A top-level import binds a name on this module, which is how every
            # package ``__init__`` publishes its surface. Removing the re-export
            # breaks ``from application_sdk.app import X`` even when X still
            # exists at its defining module.
            #
            # Only *deliberate* re-exports count. A leaf module's ``from decimal
            # import Decimal`` is an implementation detail that happens to bind a
            # name; treating it as surface would fail the gate every time an
            # unused stdlib import is tidied away. Deliberate means: listed in
            # ``__all__``, or re-exported from inside this package (relative
            # import, or an ``application_sdk.*`` absolute one).
            for bound in _reexported_names(node, declared_all, package):
                add(bound, "reexport")

    # A name served by the shim need not exist as a real binding; record the
    # remainder so the alias keeps the old name present in the snapshot.
    bound = {s.qualname for s in symbols}
    for name in sorted(aliases - bound):
        symbols.append(
            Symbol(module=module, qualname=name, kind="alias", deprecated=True)
        )
    return symbols


def build_snapshot(root: Path, package: str = DEFAULT_PACKAGE) -> Snapshot:
    """Parse every module of *package* under *root* into a :class:`Snapshot`.

    Raises :class:`ValueError` on an unreadable or unparseable module — a
    surface gate that skips files it cannot read reports a removal for every
    symbol in them, or worse, silently passes.
    """
    package_root = root / package
    if not package_root.is_dir():
        raise ValueError(f"no package {package!r} under {root}")
    snapshot = Snapshot(package=package)
    for file in iter_modules(package_root):
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - IO edge
            raise ValueError(f"cannot read {file}: {exc}") from exc
        try:
            tree = ast.parse(text, filename=str(file))
        except SyntaxError as exc:
            raise ValueError(f"cannot parse {file}: {exc}") from exc
        module = module_path(file, package_root, package)
        for symbol in extract_module(tree, module, package):
            snapshot.symbols[symbol.key] = symbol
    return snapshot


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One surface regression between two snapshots."""

    key: str
    kind: str
    """``removed`` | ``narrowed``."""
    blocking: bool
    detail: str

    def render(self) -> str:
        tier = "BLOCKING" if self.blocking else "advisory"
        return f"  [{tier}] {self.key} — {self.detail}"


def declares_break(commit_subject: str | None) -> bool:
    """True when *commit_subject* routes this change to a major bump.

    Matches the same shapes ``release-version-bump.yaml`` reads: a ``!`` before
    the colon, or a ``BREAKING CHANGE:`` trailer.
    """
    if not commit_subject:
        return False
    return bool(
        _BREAKING_SUBJECT.search(commit_subject.strip())
        or _BREAKING_TRAILER.search(commit_subject)
    )


def _narrowing(base: Symbol, head: Symbol) -> str | None:
    """Describe how *head* narrows *base*, or ``None`` if it does not.

    Narrowing is any change an existing correct call site can trip over:
    a parameter disappears or is renamed, or one that had a default stops
    having one.  Adding an optional parameter is not narrowing.
    """
    if base.kind != head.kind or head.kind not in ("function", "method"):
        return None
    dropped = [p for p in base.params if p not in head.params]
    if dropped:
        return f"parameter(s) {', '.join(dropped)} removed from {base.qualname}()"
    newly_required = [
        p
        for p in head.required_params
        if p in base.params and p not in base.required_params
    ]
    if newly_required:
        return (
            f"parameter(s) {', '.join(newly_required)} lost their default on "
            f"{base.qualname}()"
        )
    return None


def compare(
    base: Snapshot, head: Snapshot, *, commit_subject: str | None = None
) -> list[Finding]:
    """Findings for every name *head* removed or narrowed relative to *base*.

    A removal is excused when the base already marked the name deprecated —
    that is the whole policy: what you deprecated, you may later delete.
    Everything else is a finding, blocking unless the leaf name is private.
    """
    break_declared = declares_break(commit_subject)
    findings: list[Finding] = []
    for key, base_symbol in sorted(base.symbols.items()):
        head_symbol = head.symbols.get(key)
        if head_symbol is None:
            if base_symbol.deprecated:
                continue  # shipped deprecated in the base release — deletion is earned
            blocking = not base_symbol.is_private_name and not break_declared
            findings.append(
                Finding(
                    key=key,
                    kind="removed",
                    blocking=blocking,
                    detail=(
                        f"{base_symbol.kind} was importable at the base ref and is "
                        "gone at HEAD, with no deprecated alias left behind"
                    ),
                )
            )
            continue
        if base_symbol.deprecated:
            continue
        narrowed = _narrowing(base_symbol, head_symbol)
        if narrowed:
            findings.append(
                Finding(
                    key=key,
                    kind="narrowed",
                    blocking=not base_symbol.is_private_name and not break_declared,
                    detail=narrowed,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_report(
    findings: Iterable[Finding], *, base_label: str, head_label: str
) -> str:
    """A human report for the job log and the step summary."""
    findings = list(findings)
    blocking = [f for f in findings if f.blocking]
    advisory = [f for f in findings if not f.blocking]
    lines = [f"## Importable-surface check: {base_label} → {head_label}", ""]
    if not findings:
        lines.append("No names were removed or narrowed. :white_check_mark:")
        return "\n".join(lines) + "\n"
    if blocking:
        lines.append(f"### {len(blocking)} blocking removal(s)")
        lines.append("")
        lines.extend(f.render() for f in blocking)
        lines += [
            "",
            "Each of these was importable in the last release and is gone now.",
            "Two supported ways forward, per `docs/standards/symbols.md`:",
            "",
            "1. Keep the name as a deprecated alias (`@deprecated` on a def or",
            "   class, a `_DEPRECATED_CONSTANTS` entry for a module constant),",
            "   naming a replacement and a removal version. The name stays",
            "   importable, `gen-deprecations` records it, and B001 nudges the",
            "   fleet off it with no per-app work.",
            "2. If the break is deliberate and cannot be aliased, declare it in",
            "   the commit subject (`feat!:` / `BREAKING CHANGE:`) so the release",
            "   automation cuts a major rather than a patch.",
        ]
    if advisory:
        lines += ["", f"### {len(advisory)} advisory removal(s) — private names"]
        lines.append("")
        lines.extend(f.render() for f in advisory)
        lines += [
            "",
            "These are underscore-prefixed and are not a supported surface, so",
            "they do not fail the gate. They are listed because the fleet has",
            "imported SDK privates from its test suites before (FND-2388);",
            "conformance rule B008 is the consumer-side fix.",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_snapshot(args: argparse.Namespace) -> int:
    snapshot = build_snapshot(Path(args.root), args.package)
    text = snapshot.to_json()
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    print(
        f"snapshot: {len(snapshot.symbols)} symbols across {args.package}",
        file=sys.stderr,
    )
    return 0


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo* and return stdout, raising ValueError on failure."""
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ValueError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def latest_release_tag(repo: Path) -> str:
    """The newest stable ``vX.Y.Z`` tag — the surface consumers actually pinned.

    Pre-release tags (anything with a ``-`` suffix: ``v3.36.0-rc1``) are skipped:
    an rc is not what a connector's lockfile resolves to, so diffing against one
    would excuse a removal no released version ever carried.

    Raises :class:`ValueError` when no tag is reachable. That is deliberate: a
    shallow checkout with no tags would otherwise make every comparison trivially
    clean, which is the fail-open shape this gate exists to avoid. The workflow
    checks out with ``fetch-depth: 0``.
    """
    out = _git(repo, "tag", "--list", "v*", "--sort=-v:refname")
    for line in out.splitlines():
        tag = line.strip()
        if tag and "-" not in tag.removeprefix("v"):
            return tag
    raise ValueError(
        "no stable v*.*.* tag is reachable — check out with fetch-depth: 0 so the "
        "released surface can be compared against"
    )


def _resolve_commit_subject(args: argparse.Namespace) -> str | None:
    """The commit text to read a declared break from, inline or from a file.

    The file form exists for CI: a PR title and body are attacker-supplied on a
    fork PR, so the workflow writes them to a file and passes the path rather
    than interpolating either into a shell command line.
    """
    if getattr(args, "commit_subject_file", None):
        return Path(args.commit_subject_file).read_text(encoding="utf-8")
    return args.commit_subject


def _cmd_check(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    base_ref = args.base_ref or latest_release_tag(repo)
    with tempfile.TemporaryDirectory(prefix="surface-base-") as tmp:
        base_root = Path(tmp)
        # --output to a file rather than piping: the archive is binary, and a
        # text-mode pipe would mangle any non-UTF-8 byte in the tree.
        bundle = base_root / "base.tar"
        _git(
            repo,
            "archive",
            "--format=tar",
            f"--output={bundle}",
            base_ref,
            args.package,
        )
        with tarfile.open(bundle) as tar:
            tar.extractall(base_root, filter="data")
        bundle.unlink()
        base = build_snapshot(base_root, args.package)
    head = build_snapshot(repo, args.package)
    findings = compare(base, head, commit_subject=_resolve_commit_subject(args))
    report = render_report(findings, base_label=base_ref, head_label=args.head_label)
    sys.stdout.write(report)
    if args.summary_file:
        Path(args.summary_file).write_text(report, encoding="utf-8")
    return 1 if any(f.blocking for f in findings) else 0


def _cmd_compare(args: argparse.Namespace) -> int:
    base = Snapshot.from_json(Path(args.base).read_text(encoding="utf-8"))
    head = Snapshot.from_json(Path(args.head).read_text(encoding="utf-8"))
    findings = compare(base, head, commit_subject=args.commit_subject)
    report = render_report(
        findings, base_label=args.base_label, head_label=args.head_label
    )
    sys.stdout.write(report)
    if args.summary_file:
        Path(args.summary_file).write_text(report, encoding="utf-8")
    return 1 if any(f.blocking for f in findings) else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser(
        "snapshot", help="Write the importable surface of a tree as JSON."
    )
    snap.add_argument("--root", required=True, help="Repo root containing the package.")
    snap.add_argument("--package", default=DEFAULT_PACKAGE)
    snap.add_argument("-o", "--output", help="Write here instead of stdout.")
    snap.set_defaults(func=_cmd_snapshot)

    cmp_ = sub.add_parser("compare", help="Compare two snapshots and report removals.")
    cmp_.add_argument(
        "--base", required=True, help="Snapshot JSON for the released ref."
    )
    cmp_.add_argument(
        "--head", required=True, help="Snapshot JSON for the proposed tree."
    )
    cmp_.add_argument("--base-label", default="base")
    cmp_.add_argument("--head-label", default="HEAD")
    cmp_.add_argument(
        "--commit-subject",
        default=None,
        help="PR title or commit message; a declared break relaxes blocking findings.",
    )
    cmp_.add_argument(
        "--summary-file", help="Also write the report here (job summary)."
    )
    cmp_.set_defaults(func=_cmd_compare)

    chk = sub.add_parser(
        "check",
        help="Compare the working tree against the last release tag (the CI entry point).",
    )
    chk.add_argument("--repo", default=".", help="Repo root (default: cwd).")
    chk.add_argument("--package", default=DEFAULT_PACKAGE)
    chk.add_argument(
        "--base-ref",
        default=None,
        help="Ref to compare against (default: the newest stable v* tag).",
    )
    chk.add_argument("--head-label", default="HEAD")
    chk.add_argument(
        "--commit-subject",
        default=None,
        help="PR title + body; a declared break relaxes blocking findings.",
    )
    chk.add_argument(
        "--commit-subject-file",
        default=None,
        help="Read the commit text from this file instead (CI passes it this way).",
    )
    chk.add_argument("--summary-file", help="Also write the report here (job summary).")
    chk.set_defaults(func=_cmd_check)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        ValueError,
        OSError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"check_symbol_removals: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
