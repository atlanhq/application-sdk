"""P064 / P065 — offloaded work a cancel cannot stop (FND-4707)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from conformance.suite.checks import offload_cancel
from conformance.suite.checks.offload_cancel import scan_all, scan_path, scan_text


def _ids(source: str, rule: str) -> list[int]:
    return sorted(
        f.line
        for f in scan_text(textwrap.dedent(source), "app/x.py")
        if f.rule_id == rule and not f.suppressed
    )


# --------------------------------------------------------------------- P064


@pytest.mark.parametrize(
    "source",
    [
        # nested def, cursor bound by assignment
        """
        async def fetch(self, sql):
            def _q():
                cursor = self.conn.cursor()
                cursor.execute(sql)
                return cursor.fetchall()
            return await self.run_in_thread(_q)
        """,
        # module-level def, cursor bound by `with`
        """
        from application_sdk.execution.heartbeat import run_in_thread

        def _q(conn, sql):
            with conn.cursor() as cur:
                cur.executemany(sql, [])

        async def fetch(conn, sql):
            await run_in_thread(_q, conn, sql)
        """,
        # method on self, through functools.partial
        """
        import functools

        class A:
            def _q(self, sql):
                cur = self.conn.cursor()
                cur.execute(sql)

            async def fetch(self, sql):
                await self.task_context.run_in_thread(functools.partial(self._q, sql))
        """,
        # bare cursor.execute handed straight to run_in_thread
        """
        async def fetch(conn, sql):
            cursor = conn.cursor()
            await run_in_thread(cursor.execute, sql)
        """,
        # chained conn.cursor().execute inside a lambda
        """
        async def fetch(conn, sql):
            await run_in_thread(lambda: conn.cursor().execute(sql))
        """,
    ],
)
def test_p064_fires_on_an_offloaded_cursor_statement_with_no_cancel(
    source: str,
) -> None:
    assert len(_ids(source, "P064")) == 1


@pytest.mark.parametrize(
    "source",
    [
        # registers the driver cancel through current_cancel_handle()
        """
        from application_sdk.execution.heartbeat import current_cancel_handle

        async def fetch(self, sql):
            def _q():
                cursor = self.conn.cursor()
                handle = current_cancel_handle()
                if handle is not None:
                    handle.set(cursor.cancel)
                cursor.execute(sql)
            return await self.run_in_thread(_q)
        """,
        # bound CancelHandle
        """
        async def fetch(self, sql, handle):
            def _q():
                cursor = self.conn.cursor()
                cursor.execute(sql)
            return await self.run_in_thread(handle.bind(_q))
        """,
        # not a cursor: an HTTP client's execute
        """
        async def fetch(self, req):
            def _q():
                return self.http.execute(req)
            return await self.run_in_thread(_q)
        """,
        # cursor received as an argument: not followed
        """
        def _q(cursor, sql):
            cursor.execute(sql)

        async def fetch(cursor, sql):
            await run_in_thread(_q, cursor, sql)
        """,
        # function from another module: not followed
        """
        from app.queries import run_query

        async def fetch(sql):
            await run_in_thread(run_query, sql)
        """,
        # cursor statement on the event loop, not offloaded: not this rule
        """
        async def fetch(conn, sql):
            cursor = conn.cursor()
            cursor.execute(sql)
        """,
    ],
)
def test_p064_is_silent_when_cancel_is_registered_or_not_matched(source: str) -> None:
    assert _ids(source, "P064") == []


@pytest.mark.parametrize(
    "source",
    [
        # handle looked up, never set: nothing is registered
        """
        from application_sdk.execution.heartbeat import current_cancel_handle

        async def fetch(self, sql):
            def _q():
                cursor = self.conn.cursor()
                handle = current_cancel_handle()
                cursor.execute(sql)
            return await self.run_in_thread(_q)
        """,
        # handle only checked for None
        """
        from application_sdk.execution import heartbeat

        async def fetch(self, sql):
            def _q():
                cursor = self.conn.cursor()
                if heartbeat.current_cancel_handle() is None:
                    raise RuntimeError("no handle")
                cursor.execute(sql)
            return await self.run_in_thread(_q)
        """,
        # .set on something that is not the call's handle
        """
        async def fetch(self, sql):
            def _q():
                cursor = self.conn.cursor()
                self.flags.set(cursor.cancel)
                cursor.execute(sql)
            return await self.run_in_thread(_q)
        """,
    ],
)
def test_p064_fires_when_the_handle_is_looked_up_but_not_set(source: str) -> None:
    assert len(_ids(source, "P064")) == 1


@pytest.mark.parametrize(
    "source",
    [
        # set directly on the lookup
        """
        async def fetch(self, sql):
            def _q():
                cursor = self.conn.cursor()
                current_cancel_handle().set(cursor.cancel)
                cursor.execute(sql)
            return await self.run_in_thread(_q)
        """,
        # walrus-bound handle
        """
        async def fetch(self, sql):
            def _q():
                cursor = self.conn.cursor()
                if (handle := heartbeat.current_cancel_handle()) is not None:
                    handle.set(cursor.cancel)
                cursor.execute(sql)
            return await self.run_in_thread(_q)
        """,
    ],
)
def test_p064_is_silent_for_other_handle_registration_shapes(source: str) -> None:
    assert _ids(source, "P064") == []


def test_p064_reports_the_run_in_thread_line_and_honours_suppression() -> None:
    source = """
    async def fetch(self, sql):
        def _q():
            cursor = self.conn.cursor()
            cursor.execute(sql)
        await self.run_in_thread(_q)
        # conformance: ignore[P064] pymssql has no cancel; statement_timeout is set
        await self.run_in_thread(_q)
    """
    findings = [
        f for f in scan_text(textwrap.dedent(source), "app/x.py") if f.rule_id == "P064"
    ]
    assert [(f.line, f.suppressed) for f in findings] == [(6, False), (8, True)]
    assert "line 5" in findings[0].message


# --------------------------------------------------------------------- P065


@pytest.mark.parametrize(
    "source",
    [
        """
        async def run(client, q):
            for frame in await client.get_batched_results(q):
                pass
        """,
        """
        async def run(self, q):
            batches = await self.sql_client.get_batched_results(q)
            for frame in batches:
                pass
        """,
        """
        async def run(client, q):
            batches = await client.get_batched_results(q)
            return [len(f) for f in batches]
        """,
    ],
)
def test_p065_fires_on_a_sync_loop_over_batched_results(source: str) -> None:
    assert len(_ids(source, "P065")) == 1


@pytest.mark.parametrize(
    "source",
    [
        # async for: the fix
        """
        async def run(client, q):
            async for frame in await client.get_batched_results(q):
                pass
        """,
        """
        async def run(client, q):
            batches = await client.get_batched_results(q)
            return [len(f) async for f in batches]
        """,
        # handed to the writer: already correct
        """
        async def run(client, writer, q):
            await writer.write_batches(await client.get_batched_results(q))
        """,
        # rebound before the loop: not followed
        """
        async def run(client, q):
            batches = await client.get_batched_results(q)
            batches = list(batches)
            for frame in batches:
                pass
        """,
        # a sync def: no event loop to block, not this rule
        """
        def run(batches):
            for frame in batches:
                pass
        """,
        # a sync helper nested in the async def owns its own loop
        """
        async def run(client, q):
            batches = await client.get_batched_results(q)
            def _drain(it):
                for frame in it:
                    pass
            await run_in_thread(_drain, batches)
        """,
    ],
)
def test_p065_is_silent_for_async_iteration_and_unmatched_shapes(source: str) -> None:
    assert _ids(source, "P065") == []


def test_p065_honours_suppression_on_loops_and_comprehensions() -> None:
    source = """
    async def run(client, q):
        batches = await client.get_batched_results(q)
        for frame in batches:
            pass
        # conformance: ignore[P065] async engine: the result is a sync iterator
        for frame in await client.get_batched_results(q):
            pass
        # conformance: ignore[P065] frames are already materialised
        return [len(f) for f in batches]
    """
    findings = [
        f for f in scan_text(textwrap.dedent(source), "app/x.py") if f.rule_id == "P065"
    ]
    assert sorted((f.line, f.suppressed) for f in findings) == [
        (4, False),
        (7, True),
        (10, True),
    ]


# --------------------------------------------------------------------- gate

_VIOLATION = """
async def run(client, q):
    for frame in await client.get_batched_results(q):
        pass
"""


def _repo(tmp_path: Path, sdk_version: str | None) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text(_VIOLATION)
    if sdk_version is not None:
        (tmp_path / "uv.lock").write_text(
            f'[[package]]\nname = "atlan-application-sdk"\nversion = "{sdk_version}"\n'
        )
    return tmp_path


@pytest.mark.parametrize(
    ("sdk_version", "expected"),
    [
        (None, 0),
        ("3.55.1", 0),
        (".".join(map(str, offload_cancel.OFFLOAD_CANCEL_SDK_FLOOR)), 1),
        ("4.0.0", 1),
    ],
)
def test_the_check_is_silent_below_the_sdk_floor(
    tmp_path: Path, sdk_version: str | None, expected: int
) -> None:
    root = _repo(tmp_path, sdk_version)
    path = root / "app" / "x.py"
    assert len(scan_path(path, root)) == expected
    assert len(scan_all([path], root)) == expected
