"""K032 TaskPoolNotDeployed / K033 UnusedWorkerPool — ``@task(pool=)`` vs the pools map.

``@task(pool="x")`` routes the task's activities to ``atlan-<app>-<deployment>-x``
(``resolve_pool_queue`` in ``application_sdk/app/registry.py``). Only a worker
pool the chart deploys polls that queue, and the contract-toolkit renders one
for every key of ``deploy.pools`` **after the first**: the first key is the
app's main worker, which polls the base queue ``atlan-<app>-<deployment>``.
Nothing checks the two sides agree at runtime — the ``@task`` validator checks
only that the name is kebab-case — so a typo, a removed pool, or the main
worker's own key sends every call of that task to a queue no worker polls, and
the activity sits unscheduled until its timeout.

* **K032** (BLOCK) — a ``pool=`` value that names no dedicated pool. A pool is
  dedicated when it is a ``pools`` key after the first (in the contract or, with
  no contract pools, the ``pools:`` mirror in ``atlan.yaml``), a
  ``deploy.workerPools[].name`` in ``atlan.yaml``, or has an explicit
  ``ATLAN_POOL_<POOL>_QUEUE`` in the contract or ``atlan.yaml``.
* **K033** (WARN) — a dedicated ``pools`` key no ``@task(pool=)`` routes to.
  Its worker deployment, and its on-demand mirror, scale for a queue nothing is
  sent to. Skipped when any ``pool=`` value is not a string the check can
  resolve, because the unresolved one may be the pool's user.

``pool=`` values are read from string literals and from module-level string
constants of the same module (``pool=PROFILER_POOL``). Only the SDK's ``task``
decorator (``application_sdk.app.task``) counts.
"""

from __future__ import annotations

import ast
import difflib
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from conformance.suite.checks._ast_common import (
    YAML_KV_RE,
    SuppressionsMap,
    _IgnoreDirective,
    _make_pkl_finding_suppressed,
    _parse_directives,
    _parse_pkl_directives,
    contract_files,
    load_source,
    make_cli_main,
    make_finding,
    make_toml_finding,
    parse_toml_suppressions,
    relative_uri,
    safe_read_text,
    strip_yaml_comment,
)
from conformance.suite.checks._ast_common import discover as discover_python
from conformance.suite.checks._ast_common._directives_pkl import _PklIgnoreDirective
from conformance.suite.checks._ast_common._pkl import pkl_mapping_keys
from conformance.suite.schema.findings import Finding

SERIES = "K"
_NOT_DEPLOYED = "K032"
_UNUSED = "K033"

__all__ = ["SERIES", "discover", "main", "scan_all"]

# What the SDK's decorator resolves to, by ``from … import task`` or by
# attribute access on an imported module.
_TASK_DECORATOR = frozenset(
    {"application_sdk.app.task", "application_sdk.app.task.task"}
)
_POOL_OVERRIDE_RE = re.compile(r"\bATLAN_POOL_([A-Z0-9_]+)_QUEUE\b")
_DASH_NAME_RE = re.compile(r"^(?P<indent>[ ]*)-[ ]+name[ \t]*:(?P<value>.*)$")
_DASH_RE = re.compile(r"^(?P<indent>[ ]*)-[ ]")


@dataclass(frozen=True)
class _Use:
    pool: str
    file: str
    node: ast.AST
    directives: dict[int, _IgnoreDirective]


@dataclass
class _Uses:
    resolved: list[_Use] = field(default_factory=list)
    unresolved: bool = False


@dataclass(frozen=True)
class _Declared:
    """A ``pools`` key with where it is declared (``file`` is repo-relative)."""

    key: str
    file: str
    line: int


@dataclass
class _Pools:
    main: str | None = None
    dedicated: list[_Declared] = field(default_factory=list)
    """``pools`` keys after the first — each one a worker pool of its own."""
    from_contract: bool = False
    routable_extra: set[str] = field(default_factory=set)
    """Other pools a ``pool=`` may name: ``deploy.workerPools`` and overrides."""

    def routable(self) -> set[str]:
        return {d.key for d in self.dedicated} | self.routable_extra


def discover(root: Path) -> list[Path]:
    """App Python sources, for a repo with ``atlan.yaml`` or a ``contract/`` pkl.

    Without either there is no deployment shape to check ``pool=`` against.
    """
    if not ((root / "atlan.yaml").is_file() or contract_files(root)):
        return []
    return discover_python(root)


# ---------------------------------------------------------------------------
# Python side: @task(pool=...) values
# ---------------------------------------------------------------------------


def _bindings(tree: ast.Module) -> dict[str, str]:
    """Bound name -> the dotted thing it names, for every import in *tree*."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
                else:
                    root_name = alias.name.split(".")[0]
                    out[root_name] = root_name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return out


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _is_task_decorator(func: ast.expr, bindings: dict[str, str]) -> bool:
    dotted = _dotted(func)
    if dotted is None:
        return False
    head, _, rest = dotted.partition(".")
    origin = bindings.get(head)
    if origin is None:
        return False
    return (f"{origin}.{rest}" if rest else origin) in _TASK_DECORATOR


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level names bound exactly once, to a string literal."""
    values: dict[str, str | None] = {}
    for stmt in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            literal = (
                value.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
                else None
            )
            values[target.id] = None if target.id in values else literal
    return {k: v for k, v in values.items() if v is not None}


def _pool_values(tree: ast.Module, bindings: dict[str, str]) -> Iterator[ast.expr]:
    """The ``pool=`` value of every SDK ``@task(...)`` decorator in *tree*.

    ``pool=None`` is the default (no pool), so it is not yielded.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (
                isinstance(deco, ast.Call) and _is_task_decorator(deco.func, bindings)
            ):
                continue
            for kw in deco.keywords:
                none = isinstance(kw.value, ast.Constant) and kw.value.value is None
                if kw.arg == "pool" and not none:
                    yield kw.value


def _resolve_pool(value: ast.expr, constants: dict[str, str]) -> str | None:
    """A string literal, or a module-level string constant; else ``None``."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Name):
        return constants.get(value.id)
    return None


def _collect_uses(paths: list[Path], root: Path) -> _Uses:
    uses = _Uses()
    for path in paths:
        source = load_source(path)
        if source is None:
            continue
        bindings = _bindings(source.tree)
        if not any(v.startswith("application_sdk") for v in bindings.values()):
            continue
        constants = _module_constants(source.tree)
        directives: dict[int, _IgnoreDirective] | None = None
        for value in _pool_values(source.tree, bindings):
            pool = _resolve_pool(value, constants)
            if pool is None:
                uses.unresolved = True
                continue
            if directives is None:
                directives = _parse_directives(source.text)
            uses.resolved.append(
                _Use(pool, relative_uri(path, root), value, directives)
            )
    return uses


# ---------------------------------------------------------------------------
# Deployment side: the pools map, workerPools and overrides
# ---------------------------------------------------------------------------


def _yaml_value(raw: str) -> str:
    return strip_yaml_comment(raw).strip().strip("'\"")


def _yaml_pools(text: str) -> list[tuple[str, int]]:
    """``(key, line)`` of each child of the top-level ``pools:`` mapping."""
    keys: list[tuple[str, int]] = []
    in_pools = False
    child_indent: int | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = YAML_KV_RE.match(raw)
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            in_pools = m is not None and m.group("key") == "pools"
            child_indent = None
            continue
        if not in_pools or m is None:
            continue
        if child_indent is None:
            child_indent = indent
        if indent == child_indent:
            keys.append((m.group("key"), lineno))
    return keys


def _worker_pools_block(text: str) -> Iterator[str]:
    """The non-blank, non-comment lines inside ``deploy.workerPools``."""
    in_deploy = False
    wp_indent: int | None = None  # indent of the ``workerPools:`` key
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        m = YAML_KV_RE.match(raw)
        key = m.group("key") if m is not None else None
        if indent == 0:
            in_deploy, wp_indent = key == "deploy", None
        elif in_deploy and (
            wp_indent is None
            or indent < wp_indent
            or (indent == wp_indent and _DASH_RE.match(raw) is None)
        ):
            # Outside the list (or not in it yet): only a workerPools key opens it.
            wp_indent = indent if key == "workerPools" else None
        elif in_deploy:
            yield raw


def _yaml_worker_pools(text: str) -> set[str]:
    """Every ``name`` of a ``deploy.workerPools`` list item."""
    names: set[str] = set()
    item_indent: int | None = None  # indent of the keys of the current item
    for raw in _worker_pools_block(text):
        named = _DASH_NAME_RE.match(raw)
        dash = _DASH_RE.match(raw)
        m = YAML_KV_RE.match(raw)
        if named is not None:
            names.add(_yaml_value(named.group("value")))
            item_indent = len(named.group("indent")) + 2
        elif dash is not None:
            item_indent = len(dash.group("indent")) + 2
        elif (
            m is not None
            and m.group("key") == "name"
            and len(m.group("indent")) == item_indent
        ):
            names.add(_yaml_value(m.group("value")))
    names.discard("")
    return names


def _overrides(text: str) -> set[str]:
    return {g.lower().replace("_", "-") for g in _POOL_OVERRIDE_RE.findall(text)}


def _read_pools(root: Path) -> _Pools:
    pools = _Pools()
    contract_keys: list[_Declared] = []
    for path in contract_files(root):
        text = safe_read_text(path)
        if text is None:
            continue
        rel = path.relative_to(root).as_posix()
        contract_keys.extend(
            _Declared(key, rel, line)
            for key, line in pkl_mapping_keys(text, "pools", within="deploy")
        )
        pools.routable_extra |= _overrides(text)
    atlan = root / "atlan.yaml"
    atlan_text = safe_read_text(atlan) if atlan.is_file() else None
    declared = contract_keys
    if contract_keys:
        pools.from_contract = True
    elif atlan_text is not None:
        declared = [_Declared(k, "atlan.yaml", n) for k, n in _yaml_pools(atlan_text)]
    if atlan_text is not None:
        pools.routable_extra |= _yaml_worker_pools(atlan_text) | _overrides(atlan_text)
    if declared:
        pools.main = declared[0].key
        seen = {pools.main}
        for d in declared[1:]:
            if d.key not in seen:
                seen.add(d.key)
                pools.dedicated.append(d)
    return pools


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _not_deployed_message(pool: str, pools: _Pools) -> str:
    routable = sorted(pools.routable())
    if pool == pools.main:
        return (
            f'@task(pool="{pool}") names the app\'s main worker pool: "{pool}" is the '
            "first key of deploy.pools, and the first pool is the main worker, "
            "which polls the base task queue atlan-<app>-<deployment>, not "
            f"atlan-<app>-<deployment>-{pool}. Nothing polls the queue this task "
            "is routed to, so its activities sit unscheduled until they time out. "
            "Drop pool= to run the task on the main worker, or route it to a pool "
            "declared after the first key."
        )
    hint = difflib.get_close_matches(pool, routable, n=1)
    did_you_mean = f' Did you mean "{hint[0]}"?' if hint else ""
    declared = (
        "Dedicated pools: " + ", ".join(f'"{p}"' for p in routable) + "."
        if routable
        else "The app declares no dedicated worker pool."
    )
    return (
        f'@task(pool="{pool}") names no worker pool this app deploys, so its '
        f"activities go to atlan-<app>-<deployment>-{pool}, a queue no worker "
        f"polls, and sit unscheduled until they time out.{did_you_mean} "
        f"{declared} Fix the name, or declare the pool in deploy.pools of "
        "contract/app.pkl (after the first key, which is the main worker) and "
        "regenerate."
    )


def _unused_message(key: str, from_contract: bool) -> str:
    where = (
        "delete the pool from deploy.pools and regenerate atlan.yaml"
        if from_contract
        else "delete it from pools: in atlan.yaml (and from deploy.workerPools)"
    )
    return (
        f'Worker pool "{key}" is declared, but no @task(pool="{key}") routes to '
        "it: the pool's worker deployment, and its on-demand mirror, scale for a "
        f'queue no activity is sent to. Route a task to it with @task(pool="{key}"), '
        f"or {where}. If a task names it through a value "
        "this check cannot read, suppress with the reason."
    )


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Cross-check every ``@task(pool=)`` against the pools the app deploys."""
    if not paths:
        return []
    uses = _collect_uses(paths, root)
    pools = _read_pools(root)
    routable = pools.routable()
    findings: list[Finding] = [
        make_finding(
            filename=use.file,
            rule_id=_NOT_DEPLOYED,
            node=use.node,
            message=_not_deployed_message(use.pool, pools),
            directives=use.directives,
        )
        for use in uses.resolved
        if use.pool not in routable
    ]
    if uses.unresolved:
        return findings
    used = {use.pool for use in uses.resolved}
    pkl_directives: dict[str, dict[int, _PklIgnoreDirective]] = {}
    yaml_suppressions: SuppressionsMap | None = None
    for decl in pools.dedicated:
        if decl.key in used:
            continue
        message = _unused_message(decl.key, pools.from_contract)
        if pools.from_contract:
            if decl.file not in pkl_directives:
                text = safe_read_text(root / decl.file) or ""
                pkl_directives[decl.file] = _parse_pkl_directives(text)
            suppressed, justification = _make_pkl_finding_suppressed(
                rule_id=_UNUSED,
                line=decl.line,
                directives=pkl_directives[decl.file],
            )
            findings.append(
                Finding(
                    rule_id=_UNUSED,
                    file=decl.file,
                    line=decl.line,
                    column=1,
                    message=message,
                    suppressed=suppressed,
                    suppression_justification=justification,
                )
            )
        else:
            if yaml_suppressions is None:
                text = safe_read_text(root / "atlan.yaml") or ""
                yaml_suppressions = parse_toml_suppressions(text)
            findings.append(
                make_toml_finding(
                    rule_id=_UNUSED,
                    file=decl.file,
                    line=decl.line,
                    column=1,
                    message=message,
                    suppressions=yaml_suppressions,
                )
            )
    return findings


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description="K032/K033: @task(pool=) names vs the worker pools the app deploys.",
)


if __name__ == "__main__":
    sys.exit(main())
