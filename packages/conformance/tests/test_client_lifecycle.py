"""Meta-tests for the P-series client-lifecycle check (P056/P057, FND-3778).

P056 flags a ``BaseClient`` subclass that a function constructs into a local name
and never closes (no ``async with``, no ``await <name>.close()`` in a
``finally``).  P057 flags a ``close()`` override on a subclass that never calls
``super().close()``.  Both are heuristics, so each is tested to fire on the
shapes that leak and to stay silent where ownership moves, where the client is
closed on every path, and where the class is not the SDK's ``BaseClient``.

The fixtures mirror the canonical metabase app: its ``MetabaseApiClient``
overrides ``close()`` to clear the session token without the super call (P057),
and its handler closes every client it owns in a ``finally`` (clean for P056).

The check is gated on the SDK the repo's ``uv.lock`` resolves (>= 3.46.0):
``scan_text`` is the ungated AST pass, ``scan_all`` / ``scan_path`` read
``uv.lock`` and stay silent below the floor or when it cannot be read.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks._ast_common import discover
from conformance.suite.checks.client_lifecycle import (
    SERIES,
    scan_all,
    scan_path,
    scan_text,
)

_CLIENT_HEADER = (
    "from application_sdk.clients.base import BaseClient\n"
    "\n"
    "class SourceClient(BaseClient):\n"
    "    async def load(self, **kwargs):\n"
    "        pass\n"
)


def _rule(src: str, rule_id: str, file: str = "app/x.py") -> list:
    """Findings of one rule from an in-file scan of *src*."""
    return [f for f in scan_text(src, file) if f.rule_id == rule_id]


def _p056(body: str) -> list:
    """P056 findings for *body* appended below the in-file ``SourceClient``."""
    return _rule(_CLIENT_HEADER + "\n" + body, "P056")


def test_series_letter() -> None:
    assert SERIES == "P"


# ── P056 UnclosedBaseClient — fires ─────────────────────────────────────────


def test_p056_fires_when_never_closed() -> None:
    fs = _p056(
        "async def crawl(creds):\n"
        "    client = SourceClient()\n"
        "    await client.load(credentials=creds)\n"
        "    return await client.execute_http_get_request('https://x')\n"
    )
    assert len(fs) == 1
    assert fs[0].line == 8
    assert "never closed" in fs[0].message
    assert "async with SourceClient(...) as client" in fs[0].message


def test_p056_fires_when_close_is_outside_finally() -> None:
    fs = _p056(
        "async def crawl(creds):\n"
        "    client = SourceClient()\n"
        "    await client.load(credentials=creds)\n"
        "    await client.close()\n"
    )
    assert len(fs) == 1
    assert "only reached when no exception is raised" in fs[0].message


def test_p056_fires_on_annotated_binding_and_sync_def() -> None:
    fs = _p056(
        "def build_and_forget():\n"
        "    client: SourceClient = SourceClient()\n"
        "    client.timeout = 5\n"
    )
    assert len(fs) == 1


def test_p056_fires_on_transitive_subclass() -> None:
    fs = _p056(
        "class Tuned(SourceClient):\n"
        "    pass\n"
        "\n"
        "async def crawl():\n"
        "    client = Tuned()\n"
        "    await client.load()\n"
    )
    assert len(fs) == 1


def test_p056_fires_when_imported_from_package_root() -> None:
    src = (
        "from application_sdk.clients import BaseClient as SdkBase\n"
        "\n"
        "class C(SdkBase):\n"
        "    pass\n"
        "\n"
        "async def f():\n"
        "    c = C()\n"
        "    await c.load()\n"
    )
    assert len(_rule(src, "P056")) == 1


def test_p056_fires_on_module_attribute_base() -> None:
    src = (
        "from application_sdk.clients import base\n"
        "\n"
        "class C(base.BaseClient):\n"
        "    pass\n"
        "\n"
        "async def f():\n"
        "    c = C()\n"
        "    await c.load()\n"
    )
    assert len(_rule(src, "P056")) == 1


def test_p056_comparison_does_not_count_as_a_hand_off() -> None:
    fs = _p056(
        "async def f():\n"
        "    client = SourceClient()\n"
        "    if client is not None:\n"
        "        await client.load()\n"
    )
    assert len(fs) == 1


# ── P056 — silent ───────────────────────────────────────────────────────────


def test_p056_silent_when_closed_in_finally() -> None:
    assert not _p056(
        "async def crawl(creds):\n"
        "    client = SourceClient()\n"
        "    try:\n"
        "        await client.load(credentials=creds)\n"
        "    finally:\n"
        "        await client.close()\n"
    )


def test_p056_silent_when_conditionally_closed_in_finally() -> None:
    """The metabase handler shape: close the client only when this call owns it."""
    assert not _p056(
        "async def fetch(self):\n"
        "    client = SourceClient()\n"
        "    owns_client = self.client is None\n"
        "    try:\n"
        "        await client.load()\n"
        "    finally:\n"
        "        if owns_client:\n"
        "            await client.close()\n"
    )


def test_p056_silent_with_async_with_on_the_constructor() -> None:
    assert not _p056(
        "async def crawl(creds):\n"
        "    async with SourceClient() as client:\n"
        "        await client.load(credentials=creds)\n"
    )


def test_p056_silent_with_async_with_on_the_bound_name() -> None:
    assert not _p056(
        "async def crawl(creds):\n"
        "    client = SourceClient()\n"
        "    async with client:\n"
        "        await client.load(credentials=creds)\n"
    )


@pytest.mark.parametrize(
    "hand_off",
    [
        "    return client\n",
        "    yield client\n",
        "    self.client = client\n",
        "    register(client)\n",
        "    register(client=client)\n",
        "    clients.append(client)\n",
        "    return client, 1\n",
        "    pool = [client]\n",
    ],
    ids=[
        "returned",
        "yielded",
        "stored-on-self",
        "passed-positionally",
        "passed-by-keyword",
        "appended",
        "returned-in-tuple",
        "stored-in-list",
    ],
)
def test_p056_silent_when_ownership_moves(hand_off: str) -> None:
    assert not _p056(
        "async def build(self, creds):\n"
        "    client = SourceClient()\n"
        "    await client.load(credentials=creds)\n" + hand_off
    )


def test_p056_silent_when_captured_by_a_nested_scope() -> None:
    assert not _p056(
        "async def build():\n"
        "    client = SourceClient()\n"
        "    async def run():\n"
        "        await client.load()\n"
        "    return run\n"
    )


def test_p056_silent_for_attribute_and_module_bindings() -> None:
    assert not _p056(
        "shared = SourceClient()\n"
        "\n"
        "class App:\n"
        "    async def setup(self):\n"
        "        self.client = SourceClient()\n"
    )


def test_p056_silent_for_a_factory_call() -> None:
    """A factory is not a constructor; ownership is the factory caller's choice."""
    assert not _p056(
        "async def build_client():\n"
        "    client = SourceClient()\n"
        "    return client\n"
        "\n"
        "async def use():\n"
        "    client = await build_client()\n"
        "    await client.load()\n"
    )


def test_p056_silent_for_a_non_sdk_base_client() -> None:
    src = (
        "from somelib import BaseClient\n"
        "\n"
        "class C(BaseClient):\n"
        "    pass\n"
        "\n"
        "async def f():\n"
        "    c = C()\n"
        "    await c.load()\n"
    )
    assert scan_text(src, "app/x.py") == []


def test_p056_silent_for_a_plain_http_client() -> None:
    """openapi's api client wraps httpx itself and is not a BaseClient subclass."""
    src = (
        "import httpx\n"
        "\n"
        "class OpenAPIApiClient:\n"
        "    def __init__(self):\n"
        "        self._client = httpx.AsyncClient()\n"
        "\n"
        "async def f():\n"
        "    c = OpenAPIApiClient()\n"
        "    await c.fetch()\n"
    )
    assert scan_text(src, "app/x.py") == []


def test_p056_inline_suppression() -> None:
    src = (
        _CLIENT_HEADER + "\n"
        "async def f():\n"
        "    client = SourceClient()  # conformance: ignore[P056] closed by the caller's stack\n"
        "    await client.load()\n"
    )
    fs = _rule(src, "P056")
    assert len(fs) == 1 and fs[0].suppressed


# ── P057 CloseOverrideSkipsSuper ────────────────────────────────────────────

_METABASE_SHAPED_CLIENT = (
    "from application_sdk.clients.base import BaseClient\n"
    "\n"
    "class MetabaseApiClient(BaseClient):\n"
    "    async def load(self, **kwargs):\n"
    "        self.session_token = 'token'\n"
    "\n"
    "    async def close(self) -> None:\n"
    '        """Best-effort close — no logout endpoint; clear the token."""\n'
    "        self.session_token = None\n"
)


def test_p057_fires_on_metabase_shaped_override() -> None:
    fs = _rule(_METABASE_SHAPED_CLIENT, "P057")
    assert len(fs) == 1
    assert fs[0].line == 7
    assert "MetabaseApiClient.close()" in fs[0].message


@pytest.mark.parametrize(
    "call",
    [
        "await super().close()",
        "await super(MetabaseApiClient, self).close()",
        "await BaseClient.close(self)",
    ],
)
def test_p057_silent_when_base_close_is_called(call: str) -> None:
    src = _METABASE_SHAPED_CLIENT + f"        {call}\n"
    assert not _rule(src, "P057")


def test_p057_fires_on_transitive_subclass_override() -> None:
    src = (
        _CLIENT_HEADER + "\n"
        "class Tuned(SourceClient):\n"
        "    def close(self):\n"
        "        pass\n"
    )
    assert len(_rule(src, "P057")) == 1


def test_p057_silent_on_non_subclass_close() -> None:
    src = "class Pool:\n    async def close(self):\n        self.items = []\n"
    assert scan_text(src, "app/x.py") == []


# ── Cross-file resolution + SDK-version gate (repo boundary) ────────────────

_HANDLER = (
    "from app.client import MetabaseApiClient\n"
    "\n"
    "async def test_auth(creds):\n"
    "    client = MetabaseApiClient()\n"
    "    try:\n"
    "        await client.load(credentials=creds)\n"
    "    finally:\n"
    "        await client.close()\n"
    "\n"
    "async def leak(creds):\n"
    "    client = MetabaseApiClient()\n"
    "    await client.load(credentials=creds)\n"
)


def _uv_lock(sdk_version: str | None) -> str:
    """A minimal uv.lock. ``None`` omits the atlan-application-sdk entry."""
    other = (
        "[[package]]\n"
        'name = "some-dep"\n'
        'version = "1.2.3"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    if sdk_version is None:
        return other
    return (
        f"{other}\n[[package]]\n"
        'name = "atlan-application-sdk"\n'
        f'version = "{sdk_version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )


def _app(tmp_path: Path, lock: str | None) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "client.py").write_text(
        _METABASE_SHAPED_CLIENT, encoding="utf-8"
    )
    (tmp_path / "app" / "handler.py").write_text(_HANDLER, encoding="utf-8")
    if lock is not None:
        (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("version", ["3.46.0", "3.46.1", "3.47.0", "4.0.0"])
def test_resolves_subclass_across_files_at_or_above_floor(
    tmp_path: Path, version: str
) -> None:
    root = _app(tmp_path, _uv_lock(version))
    fs = scan_all(discover(root), root)
    assert sorted((f.rule_id, f.file, f.line) for f in fs) == [
        ("P056", "app/handler.py", 11),
        ("P057", "app/client.py", 7),
    ]
    # scan_path sees one file, so it cannot resolve the handler's import.
    assert scan_path(root / "app" / "handler.py", root) == []
    assert [f.rule_id for f in scan_path(root / "app" / "client.py", root)] == ["P057"]


@pytest.mark.parametrize(
    "lock",
    [
        _uv_lock("3.45.0"),
        _uv_lock("3.30.0"),
        _uv_lock(None),
        "this is : not valid = toml [",
        None,
    ],
    ids=["below-floor", "far-below", "sdk-absent", "unparseable", "no-lock"],
)
def test_silent_when_shared_pool_not_confirmed(
    tmp_path: Path, lock: str | None
) -> None:
    root = _app(tmp_path, lock)
    assert scan_all(discover(root), root) == []
    assert scan_path(root / "app" / "client.py", root) == []


def test_skips_test_files(tmp_path: Path) -> None:
    root = _app(tmp_path, _uv_lock("3.46.0"))
    path = root / "tests" / "unit" / "test_client.py"
    path.parent.mkdir(parents=True)
    path.write_text(_HANDLER, encoding="utf-8")
    assert {f.file for f in scan_all(discover(root), root)} == {
        "app/client.py",
        "app/handler.py",
    }


def test_reaches_the_runner(tmp_path: Path) -> None:
    """End to end: registered with the runner, reported for an app on >= 3.46.0."""
    import json

    from conformance.suite.runner import main as runner_main

    root = _app(tmp_path, _uv_lock("3.46.0"))
    (root / "atlan.yaml").write_text("name: example\n", encoding="utf-8")
    out = tmp_path / "report.sarif"
    runner_main(["--repo", str(root), "--series", "P", "--output", str(out)])
    results = json.loads(out.read_text(encoding="utf-8"))["runs"][0]["results"]
    assert {r["ruleId"] for r in results} >= {"P056", "P057"}
