"""Meta-tests for P060 HandRolledRetryLadder (FND-4284).

P060 flags a sleep call inside a loop that also catches exceptions — the shape
of a hand-rolled retry ladder — so new ladders do not grow back beside
``application_sdk.common.backoff``.
"""

from __future__ import annotations

from textwrap import dedent

from conformance.suite.checks.retry_ladder import RULE_ID, SERIES, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding


def _rule(src: str, file: str = "application_sdk/x.py") -> list[Finding]:
    return [f for f in scan_text(dedent(src), file) if f.rule_id == RULE_ID]


def test_series_and_catalog_entry() -> None:
    assert SERIES == "P"
    assert RULE_ID == "P060"
    rule = get_rule(RULE_ID)
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.SDK


# ── Fires ────────────────────────────────────────────────────────────────────


def test_fires_on_async_while_try_asyncio_sleep() -> None:
    fs = _rule(
        """\
        import asyncio

        async def connect():
            while True:
                try:
                    return await dial()
                except OSError:
                    pass
                await asyncio.sleep(2)
        """
    )
    assert [f.line for f in fs] == [9]
    assert "retry_async" in fs[0].message
    assert "Backoff.delay" in fs[0].message


def test_fires_on_sync_for_try_time_sleep() -> None:
    fs = _rule(
        """\
        import time

        def fetch():
            for attempt in range(3):
                try:
                    return get()
                except ValueError:
                    continue
                finally:
                    log()
                time.sleep(2 ** attempt)
        """
    )
    assert [f.line for f in fs] == [11]


def test_fires_on_sleep_in_except_block() -> None:
    fs = _rule(
        """\
        import asyncio

        async def fetch():
            for delay in (1, 2, 4):
                try:
                    return await get()
                except Exception:
                    await asyncio.sleep(delay)
        """
    )
    assert [f.line for f in fs] == [8]


def test_fires_on_async_for() -> None:
    fs = _rule(
        """\
        import asyncio

        async def drain(stream):
            async for item in stream:
                try:
                    await push(item)
                except OSError:
                    await asyncio.sleep(1)
        """
    )
    assert len(fs) == 1


def test_fires_on_bare_and_aliased_imports() -> None:
    fs = _rule(
        """\
        import asyncio as aio
        from time import sleep
        from anyio import sleep as nap

        async def a():
            while True:
                try:
                    break
                except OSError:
                    await aio.sleep(1)

        def b():
            while True:
                try:
                    break
                except OSError:
                    sleep(1)

        async def c():
            while True:
                try:
                    break
                except OSError:
                    await nap(1)
        """
    )
    assert [f.line for f in fs] == [10, 17, 24]


def test_nested_loops_report_each_sleep_once() -> None:
    fs = _rule(
        """\
        import asyncio

        async def f():
            while True:
                for _ in range(3):
                    try:
                        break
                    except OSError:
                        await asyncio.sleep(1)
        """
    )
    assert len(fs) == 1


# ── Silent ───────────────────────────────────────────────────────────────────


def test_silent_on_sleep_zero() -> None:
    assert not _rule(
        """\
        import asyncio

        async def f():
            while True:
                try:
                    step()
                except OSError:
                    pass
                await asyncio.sleep(0)
                await asyncio.sleep(0.0)
        """
    )


def test_silent_on_loop_without_try() -> None:
    assert not _rule(
        """\
        import asyncio

        async def poll():
            while not done():
                await asyncio.sleep(5)
        """
    )


def test_silent_on_try_finally_only() -> None:
    assert not _rule(
        """\
        import time

        def poll():
            while True:
                try:
                    step()
                finally:
                    cleanup()
                time.sleep(1)
        """
    )


def test_silent_when_try_or_sleep_is_in_a_nested_def() -> None:
    assert not _rule(
        """\
        import asyncio

        async def f():
            while True:
                async def attempt():
                    try:
                        return await get()
                    except OSError:
                        return None
                await asyncio.sleep(5)

        async def g():
            for _ in range(3):
                try:
                    await get()
                except OSError:
                    pass
                async def waiter():
                    await asyncio.sleep(1)
        """
    )


def test_silent_on_workflow_sleep() -> None:
    assert not _rule(
        """\
        from temporalio import workflow

        async def run():
            while True:
                try:
                    await step()
                except Exception:
                    pass
                await workflow.sleep(60)
        """
    )


def test_silent_on_the_backoff_helper_itself() -> None:
    src = """\
        import asyncio

        async def retry_async(call):
            while True:
                try:
                    return await call()
                except Exception:
                    await asyncio.sleep(1)
        """
    assert len(_rule(src)) == 1
    assert not _rule(src, file="application_sdk/common/backoff.py")
    assert not _rule(src, file="application_sdk\\common\\backoff.py")


def test_silent_on_unrelated_sleep_attribute() -> None:
    assert not _rule(
        """\
        def f(clock):
            while True:
                try:
                    step()
                except OSError:
                    clock.sleep(1)
        """
    )


# ── Suppression ──────────────────────────────────────────────────────────────


def test_suppressed_inline() -> None:
    fs = _rule(
        """\
        import asyncio

        async def poll():
            while True:
                try:
                    flush()
                except OSError:
                    log()
                await asyncio.sleep(5)  # conformance: ignore[P060] fixed flush cadence
        """
    )
    assert len(fs) == 1
    assert fs[0].suppressed
    assert fs[0].suppression_justification == "fixed flush cadence"


def test_suppressed_by_comment_line_above() -> None:
    fs = _rule(
        """\
        import asyncio

        async def poll():
            while True:
                try:
                    flush()
                except OSError:
                    log()
                # conformance: ignore[P060] fixed flush cadence
                await asyncio.sleep(5)
        """
    )
    assert len(fs) == 1 and fs[0].suppressed
