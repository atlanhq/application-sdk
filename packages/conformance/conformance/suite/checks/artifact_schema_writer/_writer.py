"""Read what a module's Python *actually writes* for a declared artifact.

The declaration side of K017 is a committed JSON file (``_declarations.py``).
This is the other side: a deliberately small, module-scoped AST read that
answers two questions about one ``FileReference`` contract field, and returns
nothing at all when it cannot answer them confidently.

* **What file extension does the writer produce?**  Resolved from the path
  expression the ``FileReference`` was built from, back through local
  assignments to a string literal.
* **What record class does the writer serialise into it?**  Resolved from the
  ``with <path>.open(...) as <handle>`` block that writes to the same path.

Three properties keep this honest, and each is a deliberate false negative:

**Module scope, never cross-module.**  A path variable is only followed within
the file it is assigned in.  Two files may both call a local ``out_dir /
"x.jsonl"``; joining them would invent a relationship that is not there.

**Any ambiguity drops the key.**  A name assigned two different extensions, a
handle opened from two different paths, a write whose argument mentions two
candidate record classes — each of those is recorded as "unknown", not resolved
by picking one.  The unit of the drop is the individual key, so an ambiguous
path variable does not silence the rest of the module.

**Only in-repo classes count as records.**  The record-class candidates are
filtered through the cross-file class registry, so a pyatlan asset, an SDK type
or a helper function's return value is invisible here rather than guessed at.
That is why the field half of this rule is quiet on a writer that serialises
through a mapper — it genuinely cannot see the shape, and says so by finding
nothing.

The shape this reads is the ordinary one::

    out_file = out_dir / "entities.jsonl"     # -> suffix ".jsonl"
    with out_file.open("wb") as f:            # -> handle f writes to out_file
        f.write(encoder.encode(record))       # -> record's class is the record
    return TransformOutput(
        transformed_entities=FileReference(   # -> field "transformed_entities"
            local_path=str(out_file),         #    is written by out_file
        ),
    )
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from conformance.suite.checks.prescriptions._error_code_prefix import ClassRecord

#: Callables that wrap a path without changing it.  Stripped before a path
#: expression is turned into a key or a suffix, so ``str(p)``, ``Path(p)`` and
#: ``p`` are all the same path.
_PATH_WRAPPERS: frozenset[str] = frozenset({"str", "Path", "PurePath", "fspath"})

#: Methods that write bytes/text to an open file handle.
_WRITE_METHODS: frozenset[str] = frozenset({"write", "writelines"})

#: Fixed-point limit for resolving a path variable assigned from another path
#: variable.  Chains this long do not occur in practice; the bound exists so a
#: cyclic assignment cannot spin.
_RESOLUTION_PASSES = 5


@dataclass(frozen=True)
class WriterSite:
    """One ``field=FileReference(...)`` binding found in a module."""

    field_name: str
    """The declared artifact field this construction populates."""

    path_key: str
    """Canonical key of the path expression the reference was built from."""

    node: ast.AST
    """Anchor for a finding — the ``FileReference`` construction itself."""


@dataclass
class ModuleWriters:
    """What one module says about the artifacts it writes."""

    sites: list[WriterSite] = field(default_factory=list)
    """Field bindings, in source order."""

    suffix_by_path: dict[str, str | None] = field(default_factory=dict)
    """Path key -> lowercased file extension; ``None`` when ambiguous."""

    records_by_path: dict[str, set[str] | None] = field(default_factory=dict)
    """Path key -> in-repo record class names written to it; ``None`` when ambiguous."""

    def suffix_for(self, path_key: str) -> str | None:
        """Return the resolved extension for *path_key*, or ``None`` if unknown."""
        return self.suffix_by_path.get(path_key)

    def records_for(self, path_key: str) -> frozenset[str]:
        """Return the record classes written to *path_key*; empty if unknown."""
        names = self.records_by_path.get(path_key)
        return frozenset(names) if names else frozenset()


# ── Path expressions ──────────────────────────────────────────────────────────


def _strip_wrappers(node: ast.expr) -> ast.expr:
    """Peel ``str()`` / ``Path()`` / ``os.fspath()`` off a path expression."""
    current = node
    for _ in range(_RESOLUTION_PASSES):
        if not isinstance(current, ast.Call) or len(current.args) != 1:
            return current
        func = current.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name not in _PATH_WRAPPERS:
            return current
        current = current.args[0]
    return current  # pragma: no cover — five nested wrappers do not occur


def path_key(node: ast.expr) -> str | None:
    """Return a canonical key identifying the file a path expression names.

    Two expressions get the same key when they are spelled the same after
    wrapper stripping, which is what makes ``str(out_file)`` at the
    ``FileReference`` and ``out_file.open(...)`` at the ``with`` resolve to one
    file.  Returns ``None`` for an expression with no stable spelling.
    """
    inner = _strip_wrappers(node)
    if isinstance(inner, (ast.Name, ast.Attribute, ast.BinOp, ast.Constant)):
        return ast.unparse(inner)
    return None


def _suffix_of_literal(text: str) -> str | None:
    """Return the lowercased extension of a **whole path** literal, or ``None``.

    Whole-path semantics, so ``PurePosixPath``'s rules apply as-is: a leading
    dot names a hidden file rather than an extension, which is why ``".env"``
    and a bare ``".jsonl"`` both resolve to nothing here.  A *fragment* of a
    path — an f-string tail, a ``with_suffix`` argument — is the opposite case
    and goes through :func:`_suffix_of_fragment` instead.
    """
    if not text or text.endswith("/"):
        return None
    suffix = PurePosixPath(text).suffix.lower()
    if not suffix:
        return None
    return suffix


def _suffix_of_fragment(text: str) -> str | None:
    """Return the lowercased extension of a path **fragment**, or ``None``.

    A fragment is the tail of a path someone else supplied the head of, so a
    leading dot is an extension rather than a hidden-file marker: ``".parquet"``
    from ``with_suffix(".parquet")`` and ``".jsonl"`` from ``f"{name}.jsonl"``
    both name the format, and whole-path rules would drop them.
    """
    if not text or "/" in text.rpartition(".")[2]:
        return None
    head, dot, tail = text.rpartition(".")
    if not dot or not tail:
        return None
    return f".{tail}".lower()


def _direct_suffix(node: ast.expr) -> str | None:
    """Resolve a path expression's extension without consulting other variables."""
    inner = _strip_wrappers(node)
    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
        return _suffix_of_literal(inner.value)
    if isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Div):
        return _direct_suffix(inner.right)
    if isinstance(inner, ast.JoinedStr):
        if not inner.values:
            return None
        tail = inner.values[-1]
        if isinstance(tail, ast.Constant) and isinstance(tail.value, str):
            # The tail is a fragment: the head is whatever the interpolations
            # produced, so ``f"{name}.jsonl"`` ends in a real extension.
            return _suffix_of_fragment(tail.value)
        return None
    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
        # ``with_suffix`` takes the extension itself, not a path containing one.
        if inner.func.attr == "with_suffix" and inner.args:
            arg = inner.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return _suffix_of_fragment(arg.value)
            return None
        if inner.func.attr in {"joinpath", "with_name"} and inner.args:
            return _direct_suffix(inner.args[-1])
    return None


# ── Module scan ───────────────────────────────────────────────────────────────


def _record(store: dict[str, str | None], key: str, value: str) -> None:
    """Record *value* for *key*, collapsing to ``None`` on disagreement."""
    if key in store and store[key] != value:
        store[key] = None
        return
    store[key] = value


def _resolve_name_suffixes(module: ast.Module, suffixes: dict[str, str | None]) -> None:
    """Follow ``a = b`` chains until every reachable path variable is resolved.

    Runs after the direct-literal pass so a variable assigned from another path
    variable inherits its extension.  Bounded by :data:`_RESOLUTION_PASSES`; a
    cycle simply stops contributing.
    """
    aliases: list[tuple[str, str]] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        source = _strip_wrappers(node.value)
        if isinstance(source, ast.Name):
            aliases.append((target.id, source.id))

    for _ in range(_RESOLUTION_PASSES):
        changed = False
        for target_name, source_name in aliases:
            source_suffix = suffixes.get(source_name)
            if source_suffix is None:
                continue
            if suffixes.get(target_name) == source_suffix:
                continue
            _record(suffixes, target_name, source_suffix)
            changed = True
        if not changed:
            return


def _assigned_value(node: ast.stmt) -> tuple[str, ast.expr] | None:
    """Return ``(name, value)`` for a single-target ``x = <expr>`` assignment."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name) and node.value is not None:
            return target.id, node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        if isinstance(node.target, ast.Name):
            return node.target.id, node.value
    return None


def _constructed_class(node: ast.expr, by_name: dict[str, ClassRecord]) -> str | None:
    """Return the in-repo class name a ``C(...)`` construction instantiates."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else None
    )
    if name is None or name not in by_name:
        return None
    return name


def _handle_target(item: ast.withitem) -> tuple[str, str] | None:
    """Return ``(handle name, path key)`` for a ``with <path>.open(...) as h``."""
    if not isinstance(item.optional_vars, ast.Name):
        return None
    ctx = item.context_expr
    if not isinstance(ctx, ast.Call):
        return None
    func = ctx.func
    if isinstance(func, ast.Attribute) and func.attr == "open":
        key = path_key(func.value)
    elif isinstance(func, ast.Name) and func.id == "open" and ctx.args:
        key = path_key(ctx.args[0])
    else:
        return None
    if key is None:
        return None
    return item.optional_vars.id, key


def _file_reference_path(
    node: ast.expr, fileref_vars: dict[str, str | None]
) -> tuple[str, ast.AST, ast.expr | None] | None:
    """Return ``(path key, anchor node, path expr)`` if *node* is a FileReference.

    Recognises ``FileReference(local_path=...)`` and
    ``FileReference.from_local(...)`` directly, and a local variable previously
    assigned one of those.  The path expression comes back alongside its key so
    a caller can read a suffix straight off an inline path — a reference built
    from a literal never passes through an assignment, so it is not in the
    path-variable map and would otherwise resolve to nothing.
    """
    if isinstance(node, ast.Name):
        key = fileref_vars.get(node.id)
        return (key, node, None) if key else None
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id == "FileReference":
        for kw in node.keywords:
            if kw.arg == "local_path":
                key = path_key(kw.value)
                return (key, node, kw.value) if key else None
        return None
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "from_local"
        and isinstance(func.value, ast.Name)
        and func.value.id == "FileReference"
    ):
        arg: ast.expr | None = node.args[0] if node.args else None
        if arg is None:
            for kw in node.keywords:
                if kw.arg == "path":
                    arg = kw.value
                    break
        if arg is None:
            return None
        key = path_key(arg)
        return (key, node, arg) if key else None
    return None


def scan_module(
    module: ast.Module,
    declared_fields: frozenset[str],
    by_name: dict[str, ClassRecord],
) -> ModuleWriters:
    """Read one module's writer facts for the app's declared artifact fields.

    Args:
        module: Parsed module AST.
        declared_fields: Field names the app's ``artifactSchemas`` declares —
            the only keyword arguments worth binding.
        by_name: Cross-file class registry, used both to confirm a construction
            is a contract class and to keep record candidates to in-repo types.

    Returns:
        The module's :class:`ModuleWriters`.  Empty when nothing resolved.
    """
    writers = ModuleWriters()

    suffixes: dict[str, str | None] = {}
    class_vars: dict[str, str | None] = {}
    fileref_vars: dict[str, str | None] = {}
    handles: dict[str, str | None] = {}

    # Pass 1 — path extensions, record-typed locals, and FileReference locals.
    for node in ast.walk(module):
        assigned = _assigned_value(node) if isinstance(node, ast.stmt) else None
        if assigned is not None:
            name, value = assigned
            direct = _direct_suffix(value)
            if direct is not None:
                _record(suffixes, name, direct)
            cls = _constructed_class(value, by_name)
            if cls is not None:
                _record(class_vars, name, cls)
            ref = _file_reference_path(value, {})
            if ref is not None:
                _record(fileref_vars, name, ref[0])
                if ref[2] is not None:
                    inline = _direct_suffix(ref[2])
                    if inline is not None:
                        _record(suffixes, ref[0], inline)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                bound = _handle_target(item)
                if bound is not None:
                    _record(handles, bound[0], bound[1])

    _resolve_name_suffixes(module, suffixes)
    writers.suffix_by_path = suffixes

    # Pass 2 — what gets written through each resolved handle.
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _WRITE_METHODS:
            continue
        if not isinstance(func.value, ast.Name):
            continue
        key = handles.get(func.value.id)
        if key is None or not node.args:
            continue
        candidates = _record_candidates(node.args[0], class_vars, by_name)
        if len(candidates) != 1:
            # Nothing, or more than one plausible record type: unknown, and an
            # unknown must not be resolved by choosing.
            if candidates:
                writers.records_by_path[key] = None
            continue
        if key in writers.records_by_path and writers.records_by_path[key] is None:
            continue
        writers.records_by_path.setdefault(key, set())
        bucket = writers.records_by_path[key]
        if bucket is not None:
            bucket |= candidates

    # Pass 3 — bind declared fields to the path their FileReference names.
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if _constructed_class(node, by_name) is None:
            continue  # Not a construction of an in-repo class — not a contract.
        for kw in node.keywords:
            if kw.arg is None or kw.arg not in declared_fields:
                continue
            ref = _file_reference_path(kw.value, fileref_vars)
            if ref is None:
                continue
            key, anchor, path_expr = ref
            # An inline path never passes through an assignment, so seed its
            # suffix here; `_record` keeps an already-ambiguous key ambiguous.
            if path_expr is not None:
                inline_suffix = _direct_suffix(path_expr)
                if inline_suffix is not None:
                    _record(suffixes, key, inline_suffix)
            writers.sites.append(
                WriterSite(field_name=kw.arg, path_key=key, node=anchor)
            )

    writers.sites.sort(key=lambda s: (getattr(s.node, "lineno", 0), s.field_name))
    return writers


def _record_candidates(
    node: ast.expr,
    class_vars: dict[str, str | None],
    by_name: dict[str, ClassRecord],
) -> set[str]:
    """Return the in-repo record classes a write expression could be serialising.

    Walks the whole argument expression, so ``encoder.encode(rec) + b"\\n"``
    and ``json.dumps(Record(...))`` both resolve.  ``FileReference`` is excluded:
    it is the hand-off token, never the payload.
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            cls = _constructed_class(child, by_name)
            if cls is not None and cls != "FileReference":
                found.add(cls)
        elif isinstance(child, ast.Name):
            cls = class_vars.get(child.id)
            if cls is not None and cls != "FileReference":
                found.add(cls)
    return found
