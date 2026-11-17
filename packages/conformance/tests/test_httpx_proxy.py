"""Meta-tests for the P-series httpx-proxy check (P066).

P066 flags an ``httpx.AsyncHTTPTransport`` / ``httpx.HTTPTransport`` built
without ``proxy=``: httpx skips HTTPS_PROXY / NO_PROXY for any client given an
explicit transport, so the app's requests bypass the proxy on proxy-only hosts.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.httpx_proxy import SERIES, scan_path, scan_text


def _rule(src: str, file: str = "app/client.py") -> list:
    return [f for f in scan_text(src, file) if f.rule_id == "P066"]


def test_series_letter() -> None:
    assert SERIES == "P"


# ── fires ────────────────────────────────────────────────────────────────────


def test_fires_on_async_transport_via_module() -> None:
    src = (
        "import httpx\n"
        "async def f(url, ctx):\n"
        "    transport = httpx.AsyncHTTPTransport(verify=ctx)\n"
        "    async with httpx.AsyncClient(transport=transport) as c:\n"
        "        return await c.get(url)\n"
    )
    [finding] = _rule(src)
    assert finding.line == 3
    assert "httpx.AsyncHTTPTransport" in finding.message
    assert "proxy=" in finding.message


def test_fires_on_sync_transport() -> None:
    src = "import httpx\nt = httpx.HTTPTransport(retries=3)\n"
    assert len(_rule(src)) == 1


def test_fires_on_module_alias() -> None:
    src = "import httpx as hx\nt = hx.AsyncHTTPTransport(limits=lim)\n"
    assert len(_rule(src)) == 1


def test_fires_on_direct_import_and_alias() -> None:
    src = (
        "from httpx import AsyncHTTPTransport, HTTPTransport as T\n"
        "a = AsyncHTTPTransport()\n"
        "b = T()\n"
    )
    assert [f.line for f in _rule(src)] == [2, 3]


def test_fires_on_transport_inline_in_client_kwargs() -> None:
    """The shape a production RCA hit: a transport built inline in a dict."""
    src = (
        "import httpx\n"
        "kwargs = {\n"
        '    "transport": httpx.AsyncHTTPTransport(verify=v, limits=lim),\n'
        "}\n"
    )
    assert len(_rule(src)) == 1


def test_fires_on_transport_wrapped_by_retry_library() -> None:
    src = (
        "import httpx\n"
        "from httpx_retries import RetryTransport\n"
        "t = RetryTransport(transport=httpx.AsyncHTTPTransport(verify=ctx))\n"
    )
    assert len(_rule(src)) == 1


# ── silent ───────────────────────────────────────────────────────────────────


def test_silent_with_explicit_proxy() -> None:
    src = (
        "import httpx\nt = httpx.AsyncHTTPTransport(verify=ctx, proxy=env_proxy(url))\n"
    )
    assert _rule(src) == []


def test_silent_with_explicit_proxy_none() -> None:
    """``proxy=None`` is a deliberate choice (a localhost-only transport)."""
    src = "import httpx\nt = httpx.AsyncHTTPTransport(proxy=None)\n"
    assert _rule(src) == []


def test_silent_with_kwargs_splat() -> None:
    src = "import httpx\nt = httpx.AsyncHTTPTransport(**transport_kwargs)\n"
    assert _rule(src) == []


def test_silent_on_client_without_transport() -> None:
    src = "import httpx\nc = httpx.AsyncClient(verify=ctx, limits=lim)\n"
    assert _rule(src) == []


def test_silent_on_mock_transport() -> None:
    src = "import httpx\nt = httpx.MockTransport(handler)\n"
    assert _rule(src) == []


def test_silent_on_subclass_definition_and_super_init() -> None:
    src = (
        "import httpx\n"
        "class T(httpx.AsyncHTTPTransport):\n"
        "    def __init__(self, **kw):\n"
        "        super().__init__(**kw)\n"
    )
    assert _rule(src) == []


def test_silent_on_same_name_from_other_library() -> None:
    src = "from mylib import AsyncHTTPTransport\nt = AsyncHTTPTransport()\n"
    assert _rule(src) == []


def test_silent_when_parameter_shadows_module_alias() -> None:
    """Bindings are scope-local: a parameter named like the alias is not httpx."""
    src = "import httpx as h\ndef f(h):\n    return h.AsyncHTTPTransport()\n"
    assert _rule(src) == []


def test_silent_when_parameter_shadows_imported_ctor() -> None:
    src = (
        "from httpx import AsyncHTTPTransport\n"
        "def f(AsyncHTTPTransport):\n"
        "    return AsyncHTTPTransport()\n"
    )
    assert _rule(src) == []


def test_silent_when_local_assignment_shadows_module() -> None:
    src = (
        "import httpx\n"
        "def f():\n"
        "    httpx = other_lib\n"
        "    return httpx.AsyncHTTPTransport()\n"
    )
    assert _rule(src) == []


def test_fires_in_nested_function_that_does_not_shadow() -> None:
    """Shadowing in one function does not hide the module from its siblings."""
    src = (
        "import httpx as h\n"
        "def shadowed(h):\n"
        "    return h.AsyncHTTPTransport()\n"
        "def real():\n"
        "    def inner():\n"
        "        return h.AsyncHTTPTransport()\n"
        "    return inner()\n"
    )
    assert [f.line for f in _rule(src)] == [6]


def test_fires_on_function_local_import() -> None:
    src = "def f():\n    import httpx\n    return httpx.HTTPTransport()\n"
    assert [f.line for f in _rule(src)] == [3]


def test_silent_without_httpx_import() -> None:
    src = "t = httpx.AsyncHTTPTransport()\n"
    assert _rule(src) == []


def test_silent_on_syntax_error() -> None:
    assert _rule("import httpx\nt = httpx.AsyncHTTPTransport(\n") == []


# ── suppression ──────────────────────────────────────────────────────────────


def test_inline_suppression() -> None:
    src = (
        "import httpx\n"
        "t = httpx.AsyncHTTPTransport()  "
        "# conformance: ignore[P066] sidecar on localhost only\n"
    )
    [finding] = _rule(src)
    assert finding.suppressed


def test_suppression_on_comment_line_above() -> None:
    src = (
        "import httpx\n"
        "# conformance: ignore[P066] sidecar on localhost only\n"
        "t = httpx.AsyncHTTPTransport()\n"
    )
    [finding] = _rule(src)
    assert finding.suppressed


# ── path scanning ────────────────────────────────────────────────────────────


def test_scan_path_reports_repo_relative_uri(tmp_path: Path) -> None:
    target = tmp_path / "app" / "client.py"
    target.parent.mkdir()
    target.write_text(
        "import httpx\nt = httpx.AsyncHTTPTransport()\n", encoding="utf-8"
    )

    [finding] = [f for f in scan_path(target, tmp_path) if f.rule_id == "P066"]
    assert finding.file == "app/client.py"
