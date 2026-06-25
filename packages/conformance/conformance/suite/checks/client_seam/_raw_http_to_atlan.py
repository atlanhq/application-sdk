"""P019 RawHttpToAtlan — raw HTTP aimed at an Atlan service instead of pyatlan.

``pyatlan`` is the supported client for Atlan services; the SDK exposes it via
``application_sdk.credentials`` (``AtlanClientMixin.get_or_create_async_atlan_client``
/ ``create_async_atlan_client``).  This detector flags a raw HTTP request —
``httpx`` / ``requests`` / ``aiohttp`` request method, or
``urllib.request.urlopen`` / ``Request`` — whose URL statically carries an Atlan
API marker (``/api/meta`` = Atlas, ``/api/service`` = Heracles) (BLDX-1430).

It is intentionally a heuristic.  pyatlan hides endpoint routing, so the only
static "this targets Atlan" signal is the URL path; a URL assembled entirely
from runtime variables is an accepted false-negative.  The marker requirement is
what keeps it low-FP: legitimate non-Atlan HTTP (Dapr ``/v1.0``, Segment,
Prometheus, Temporal metrics, third-party connector APIs, proxy detection,
``urllib.parse`` helpers) carries no such marker.  Constructing pyatlan's own
``AtlanClient`` / ``AsyncAtlanClient`` is *not* flagged — that is blessed reuse.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

# Atlan service path markers — the only static signal that a request targets
# Atlan (pyatlan hides host routing, and product code has no Atlan host const).
_ATLAN_MARKERS: tuple[str, ...] = ("/api/meta", "/api/service")

# Modules whose presence means the file does raw HTTP itself.
_HTTP_MODULES: frozenset[str] = frozenset(
    {"httpx", "requests", "aiohttp", "urllib.request", "http.client"}
)

# Request entry points by callable name.
_HTTP_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request"}
)
_URLLIB_FUNCS: frozenset[str] = frozenset({"urlopen", "Request"})
# Client/session constructors that take a ``base_url=`` we can inspect.
_CLIENT_CTORS: frozenset[str] = frozenset({"Client", "AsyncClient", "ClientSession"})


def _static_str(expr: ast.expr) -> str:
    """Best-effort concatenation of an expression's *static* string parts.

    Handles a plain ``str`` constant, an f-string's literal segments
    (``FormattedValue`` runtime parts are skipped), and ``+`` string
    concatenation.  Anything else contributes the empty string.  This is enough
    to spot an Atlan path marker baked into a URL literal/f-string/concat.
    """
    if isinstance(expr, ast.Constant):
        return expr.value if isinstance(expr.value, str) else ""
    if isinstance(expr, ast.JoinedStr):
        return "".join(
            part.value
            for part in expr.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _static_str(expr.left) + _static_str(expr.right)
    return ""


def _targets_atlan(expr: ast.expr | None) -> bool:
    """True if *expr*'s static string content names an Atlan service path."""
    if expr is None:
        return False
    text = _static_str(expr)
    return any(marker in text for marker in _ATLAN_MARKERS)


def _url_index(method: str) -> int:
    """Positional index of the URL argument for a request callable.

    Everything takes the URL first (``get(url)``, ``urlopen(url)``,
    ``Request(url)``) except ``request(method, url, ...)``, where it is second.
    """
    return 1 if method == "request" else 0


def _kwarg(node: ast.Call, name: str) -> ast.expr | None:
    """Return the value of keyword *name*, or ``None``."""
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _positional_or_kw(node: ast.Call, index: int, keyword: str) -> ast.expr | None:
    """Return the positional arg at *index*, else the *keyword* arg."""
    if len(node.args) > index and not isinstance(node.args[index], ast.Starred):
        return node.args[index]
    return _kwarg(node, keyword)


def _collect_http_imports(
    tree: ast.AST,
) -> tuple[set[str], set[str], set[str], bool]:
    """Collect raw-HTTP import provenance from *tree*.

    Returns ``(module_aliases, func_names, ctor_names, uses_http)`` where:

    * ``module_aliases`` — names bound to a raw-HTTP module
      (``import httpx`` / ``import urllib.request as ur`` / ``from urllib import request``);
    * ``func_names`` — directly-imported request callables
      (``from httpx import get``, ``from urllib.request import urlopen``);
    * ``ctor_names`` — directly-imported client/session constructors
      (``from httpx import AsyncClient``);
    * ``uses_http`` — whether the file imports any raw-HTTP surface at all
      (the file-level gate that keeps unrelated ``.get(...)`` calls silent).
    """
    module_aliases: set[str] = set()
    func_names: set[str] = set()
    ctor_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name in _HTTP_MODULES or name in {"httpx", "requests", "aiohttp"}:
                    # ``import httpx`` binds ``httpx``; ``import urllib.request``
                    # binds top-level ``urllib`` unless aliased.
                    bound = alias.asname or name.split(".")[0]
                    module_aliases.add(bound)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                continue
            module = node.module or ""
            if module == "urllib" and any(a.name == "request" for a in node.names):
                # ``from urllib import request`` → ``request`` is the http module.
                for alias in node.names:
                    if alias.name == "request":
                        module_aliases.add(alias.asname or "request")
            if module in _HTTP_MODULES:
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if alias.name in _HTTP_METHODS or alias.name in _URLLIB_FUNCS:
                        func_names.add(bound)
                    elif alias.name in _CLIENT_CTORS:
                        ctor_names.add(bound)

    uses_http = bool(module_aliases or func_names or ctor_names)
    return module_aliases, func_names, ctor_names, uses_http


def _url_arg(
    node: ast.Call,
    module_aliases: set[str],
    func_names: set[str],
    ctor_names: set[str],
) -> ast.expr | None:
    """Return the URL/``base_url`` argument if *node* is a raw-HTTP request call.

    ``None`` if the call is not a recognised raw-HTTP request entry point.
    """
    func = node.func

    if isinstance(func, ast.Attribute):
        if func.attr in _HTTP_METHODS or func.attr in _URLLIB_FUNCS:
            # ``requests.get(url)``, ``client.post(url)``, ``urllib.request.urlopen(url)``
            return _positional_or_kw(node, _url_index(func.attr), "url")
        if func.attr in _CLIENT_CTORS and _ref_is_http_module(
            func.value, module_aliases
        ):
            # ``httpx.AsyncClient(base_url=...)`` / ``aiohttp.ClientSession(base_url=...)``
            return _kwarg(node, "base_url")
        return None

    if isinstance(func, ast.Name):
        if func.id in func_names:
            # ``get(url)`` / ``urlopen(url)`` / ``Request(url)`` imported directly
            return _positional_or_kw(node, _url_index(func.id), "url")
        if func.id in ctor_names:
            # ``AsyncClient(base_url=...)`` imported directly
            return _kwarg(node, "base_url")

    return None


def _ref_is_http_module(value: ast.expr, module_aliases: set[str]) -> bool:
    """True if *value* is a reference to a known raw-HTTP module alias.

    Covers ``httpx.AsyncClient`` (``value`` is ``Name('httpx')``) and
    ``urllib.request.Request`` style chains (``value`` ends in a known alias).
    """
    if isinstance(value, ast.Name):
        return value.id in module_aliases
    if isinstance(value, ast.Attribute):
        return value.attr in module_aliases or _ref_is_http_module(
            value.value, module_aliases
        )
    return False


def check_p017(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P019 for raw HTTP requests whose URL names an Atlan service path."""
    module_aliases, func_names, ctor_names, uses_http = _collect_http_imports(tree)
    if not uses_http:
        # No raw-HTTP surface imported — nothing this rule cares about.
        return []

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        url_expr = _url_arg(node, module_aliases, func_names, ctor_names)
        if url_expr is None or not _targets_atlan(url_expr):
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P019",
                node=node,
                message=(
                    "Raw HTTP request to an Atlan endpoint (/api/meta or "
                    "/api/service) — pyatlan is the supported Atlan client. Use it "
                    "via application_sdk.credentials "
                    "(AtlanClientMixin.get_or_create_async_atlan_client or "
                    "create_async_atlan_client) instead of hand-rolling HTTP. If this "
                    "is genuinely unavoidable, justify it with "
                    "'# conformance: ignore[P019] <reason>'."
                ),
                directives=directives,
            )
        )
    return findings
