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
keyword, resolving each name in the call's own scope (a parameter or local
that shadows the import is not httpx).  Any explicit ``proxy=`` — including ``proxy=None`` for a transport
that only ever reaches localhost — is a deliberate decision and stays silent,
as does a call that splats ``**kwargs`` (the proxy may be in it).  Subclasses
of the transport, and ``httpx.MockTransport``, are not matched.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import (
    UNTRUSTED,
    NameResolver,
    _IgnoreDirective,
    make_finding,
    scoped_calls,
)
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


#: Binding kind of a name that is the ``httpx`` module itself.
_HTTPX_MODULE = "httpx-module"


def _classify_import(node: ast.Import | ast.ImportFrom) -> dict[str, str]:
    """Map each name *node* binds to ``httpx-module``, a transport ctor, or other."""
    bound: dict[str, str] = {}
    if isinstance(node, ast.ImportFrom):
        from_httpx = node.level == 0 and node.module == "httpx"
        for alias in node.names:
            name = alias.asname or alias.name
            if from_httpx and alias.name in _TRANSPORT_CTORS:
                bound[name] = f"httpx.{alias.name}"
            else:
                bound[name] = UNTRUSTED
    else:
        for alias in node.names:
            if alias.asname is not None:
                is_httpx = alias.name == "httpx"
                bound[alias.asname] = _HTTPX_MODULE if is_httpx else UNTRUSTED
            else:
                # ``import httpx.x`` binds ``httpx`` to the package itself.
                root = alias.name.split(".")[0]
                bound[root] = _HTTPX_MODULE if root == "httpx" else UNTRUSTED
    return bound


def _transport_ctor(call: ast.Call, resolve: NameResolver) -> str | None:
    """The httpx transport class *call* constructs in its own scope, or ``None``.

    Names resolve the way Python resolves them, so after ``import httpx as h``
    a function's own ``h`` parameter is not the httpx module.
    """
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _TRANSPORT_CTORS
        and isinstance(func.value, ast.Name)
        and resolve(func.value.id) == _HTTPX_MODULE
    ):
        return f"httpx.{func.attr}"
    if isinstance(func, ast.Name):
        kind = resolve(func.id)
        if (
            kind is not None
            and kind.startswith("httpx.")
            and kind[6:] in _TRANSPORT_CTORS
        ):
            return kind
    return None


def _sets_proxy(call: ast.Call) -> bool:
    """True if *call* passes ``proxy=`` or splats ``**kwargs`` that may carry it."""
    return any(kw.arg in (None, "proxy") for kw in call.keywords)


def check_p066(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Flag httpx transport constructions that pass no ``proxy=``."""
    findings: list[Finding] = []
    for node, name in scoped_calls(tree, _classify_import, _transport_ctor):
        if _sets_proxy(node):
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
