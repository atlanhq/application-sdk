"""P066 HttpxTransportWithoutProxy — an httpx transport built without ``proxy=``.

httpx applies ``HTTP(S)_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY`` only when the
client builds its own transport: in ``httpx/_client.py``,
``allow_env_proxies = trust_env and transport is None``.  An app that builds
``httpx.AsyncHTTPTransport(...)`` (typically to put ``verify=``, ``limits=`` or
``retries=`` on it) and hands it to a client has therefore opted out of the
environment proxy, whether it meant to or not.  On a host whose egress is
proxy-only — common for self-deployed runtimes — every request connects
directly and times out with no HTTP response.

The detector flags a call to ``httpx.AsyncHTTPTransport`` or
``httpx.HTTPTransport`` (through ``import httpx [as x]`` or
``from httpx import AsyncHTTPTransport [as y]``) that passes no ``proxy=``
keyword.  Any explicit ``proxy=`` — including ``proxy=None`` for a transport
that only ever reaches localhost — is a deliberate decision and stays silent,
as does a call that splats ``**kwargs`` (the proxy may be in it).  Subclasses
of the transport, and ``httpx.MockTransport``, are not matched.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

RULE_ID = "P066"

#: httpx transport constructors that ignore the environment proxy.
_TRANSPORT_CTORS: frozenset[str] = frozenset({"AsyncHTTPTransport", "HTTPTransport"})

_MESSAGE = (
    "`{name}(...)` is built without `proxy=`. httpx ignores HTTPS_PROXY / "
    "NO_PROXY for any client given an explicit transport, so on a proxy-only "
    "host these requests connect directly and time out. Pass `proxy=` resolved "
    "from the environment (respecting NO_PROXY), or let the client build its own "
    "transport."
)


def _collect_imports(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Return ``(httpx module aliases, {local name: transport ctor})``."""
    modules: set[str] = set()
    ctors: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "httpx":
                    modules.add(alias.asname or "httpx")
        elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
            for alias in node.names:
                if alias.name in _TRANSPORT_CTORS:
                    ctors[alias.asname or alias.name] = alias.name
    return modules, ctors


def _transport_ctor(
    call: ast.Call, modules: set[str], ctors: dict[str, str]
) -> str | None:
    """The httpx transport class *call* constructs, or ``None``."""
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _TRANSPORT_CTORS
        and isinstance(func.value, ast.Name)
        and func.value.id in modules
    ):
        return f"httpx.{func.attr}"
    if isinstance(func, ast.Name) and func.id in ctors:
        return f"httpx.{ctors[func.id]}"
    return None


def _sets_proxy(call: ast.Call) -> bool:
    """True if *call* passes ``proxy=`` or splats ``**kwargs`` that may carry it."""
    return any(kw.arg in (None, "proxy") for kw in call.keywords)


def check_p066(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Flag httpx transport constructions that pass no ``proxy=``."""
    modules, ctors = _collect_imports(tree)
    if not modules and not ctors:
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _transport_ctor(node, modules, ctors)
        if name is None or _sets_proxy(node):
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id=RULE_ID,
                node=node,
                message=_MESSAGE.format(name=name),
                directives=directives,
            )
        )
    return findings
