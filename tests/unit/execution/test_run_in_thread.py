"""Tests for run_in_thread ContextVar propagation."""

import contextvars

import pytest

from application_sdk.execution.heartbeat import run_in_thread

_test_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_test_var", default="unset"
)


def _read_contextvar() -> str:
    """Blocking function that reads a ContextVar — runs in worker thread."""
    return _test_var.get()


@pytest.mark.asyncio
async def test_run_in_thread_propagates_contextvars():
    """ContextVars set in the calling coroutine must be visible inside run_in_thread."""
    token = _test_var.set("propagated")
    try:
        result = await run_in_thread(_read_contextvar)
        assert result == "propagated"
    finally:
        _test_var.reset(token)


@pytest.mark.asyncio
async def test_run_in_thread_does_not_leak_back():
    """Mutations inside the thread must NOT leak back to the caller."""

    def _mutate_contextvar() -> str:
        _test_var.set("mutated-in-thread")
        return _test_var.get()

    token = _test_var.set("original")
    try:
        thread_result = await run_in_thread(_mutate_contextvar)
        assert thread_result == "mutated-in-thread"
        assert _test_var.get() == "original"
    finally:
        _test_var.reset(token)


@pytest.mark.asyncio
async def test_run_in_thread_default_value():
    """Without setting the ContextVar, the thread sees the default."""
    result = await run_in_thread(_read_contextvar)
    assert result == "unset"
