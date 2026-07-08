"""Tests for App.iterate() — the durable page-iteration driver.

These exercise the driver's contract without a Temporal worker: the page-task is
called directly (the @task decorator only attaches metadata), and continue-as-new
is simulated by patching continue_with / workflow.info.
"""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app.base import App, AppContextError
from application_sdk.app.base_errors import DurableIterateInvalidArgumentError
from application_sdk.app.context import AppContext
from application_sdk.contracts.base import Input, Output

# =============================================================================
# Fixtures
# =============================================================================


class PageIn(Input, allow_unbounded_fields=True):
    token: int = 0


class PageOut(Output, allow_unbounded_fields=True):
    processed: int = 0
    next_token: int | None = None


class RunIn(Input, allow_unbounded_fields=True):
    token: int | None = None


class RunOut(Output, allow_unbounded_fields=True):
    total: int = 0


class _Stop(Exception):
    """Sentinel raised by a mocked continue_with to mimic ContinueAsNewError."""


@pytest.fixture(autouse=True)
def _reset(clean_app_registry, clean_task_registry):  # type: ignore[no-untyped-def]
    yield


def _ctx() -> AppContext:
    return AppContext(
        app_name="iter-app",
        app_version="0.0.1",
        run_id="rid",
        correlation_id="corr-1",
    )


def _make_app(pages: int | None):
    """Build an App whose page-task walks tokens 0,1,2,...

    If ``pages`` is None the source is infinite (next_token always advances);
    otherwise it is exhausted (next_token=None) once ``pages`` have been served.
    """

    class _IterApp(App):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[int] = []

        async def run(self, input: RunIn) -> RunOut:  # pragma: no cover - unused
            return RunOut()

        async def crawl(self, input: PageIn) -> PageOut:
            self.seen.append(input.token)
            if pages is not None and input.token >= pages - 1:
                nxt = None
            else:
                nxt = input.token + 1
            return PageOut(processed=10, next_token=nxt)

    return _IterApp()


def _iterate(app: App, **overrides):
    kwargs = dict(
        cursor=0,
        input_factory=lambda c: PageIn(token=c or 0),
        next_cursor=lambda out: out.next_token,
        resume_input=lambda c: RunIn(token=c),
    )
    kwargs.update(overrides)
    return app.iterate(app.crawl, **kwargs)  # type: ignore[attr-defined]


# =============================================================================
# Happy path — runs to completion without continue-as-new
# =============================================================================


class TestCompletion:
    @pytest.mark.asyncio
    async def test_single_page_source(self) -> None:
        app = _make_app(pages=1)
        app._context = _ctx()
        out = await _iterate(app)
        assert app.seen == [0]  # type: ignore[attr-defined]
        assert out.next_token is None

    @pytest.mark.asyncio
    async def test_multi_page_runs_to_completion_in_order(self) -> None:
        app = _make_app(pages=3)
        app._context = _ctx()
        out = await _iterate(app)
        # Cursor advanced 0 -> 1 -> 2, then exhausted.
        assert app.seen == [0, 1, 2]  # type: ignore[attr-defined]
        assert out.next_token is None

    @pytest.mark.asyncio
    async def test_empty_source_returns_first_output(self) -> None:
        # A source that is immediately exhausted still runs the first page once
        # (the page-task is the only thing that knows it is empty).
        app = _make_app(pages=1)
        app._context = _ctx()
        out = await _iterate(app)
        assert isinstance(out, PageOut)


# =============================================================================
# Continue-as-new triggers
# =============================================================================


class TestContinueAsNew:
    @pytest.mark.asyncio
    async def test_max_pages_per_generation_triggers_continue_with(self) -> None:
        app = _make_app(pages=None)  # infinite source
        app._context = _ctx()
        with mock.patch.object(app, "continue_with", side_effect=_Stop) as cont:
            with pytest.raises(_Stop):
                await _iterate(app, max_pages_per_generation=2)
        # Two pages fully processed BEFORE the boundary (await-before-CAN),
        # then continue-as-new with the cursor pointing past them.
        assert app.seen == [0, 1]  # type: ignore[attr-defined]
        cont.assert_called_once()
        assert cont.call_args.args[0] == RunIn(token=2)

    @pytest.mark.asyncio
    async def test_is_continue_as_new_suggested_triggers(self) -> None:
        app = _make_app(pages=None)
        app._context = _ctx()
        info = mock.MagicMock()
        info.is_continue_as_new_suggested.return_value = True
        with (
            mock.patch("application_sdk.app.base._is_in_workflow", return_value=True),
            mock.patch("application_sdk.app.base.workflow.info", return_value=info),
            mock.patch.object(app, "continue_with", side_effect=_Stop) as cont,
        ):
            with pytest.raises(_Stop):
                await _iterate(app)
        # First page processed, then Temporal-suggested CAN.
        assert app.seen == [0]  # type: ignore[attr-defined]
        cont.assert_called_once()
        assert cont.call_args.args[0] == RunIn(token=1)

    @pytest.mark.asyncio
    async def test_history_threshold_triggers(self) -> None:
        app = _make_app(pages=None)
        app._context = _ctx()
        info = mock.MagicMock()
        info.is_continue_as_new_suggested.return_value = False
        info.get_current_history_length.return_value = 20_000
        with (
            mock.patch("application_sdk.app.base._is_in_workflow", return_value=True),
            mock.patch("application_sdk.app.base.workflow.info", return_value=info),
            mock.patch.object(app, "continue_with", side_effect=_Stop) as cont,
        ):
            with pytest.raises(_Stop):
                await _iterate(app, history_threshold=15_000)
        assert app.seen == [0]  # type: ignore[attr-defined]
        cont.assert_called_once()

    @pytest.mark.asyncio
    async def test_below_history_threshold_does_not_trigger(self) -> None:
        app = _make_app(pages=3)  # finite, so it completes on its own
        app._context = _ctx()
        info = mock.MagicMock()
        info.is_continue_as_new_suggested.return_value = False
        info.get_current_history_length.return_value = 100
        with (
            mock.patch("application_sdk.app.base._is_in_workflow", return_value=True),
            mock.patch("application_sdk.app.base.workflow.info", return_value=info),
            mock.patch.object(app, "continue_with", side_effect=_Stop) as cont,
        ):
            out = await _iterate(app, history_threshold=15_000)
        cont.assert_not_called()
        assert app.seen == [0, 1, 2]  # type: ignore[attr-defined]
        assert out.next_token is None

    @pytest.mark.asyncio
    async def test_resume_input_carries_cursor(self) -> None:
        # After continue-as-new, run() would be re-entered with the resumed input;
        # a subsequent iterate(cursor=<resumed>) picks up exactly there.
        app = _make_app(pages=None)
        app._context = _ctx()
        with mock.patch.object(app, "continue_with", side_effect=_Stop) as cont:
            with pytest.raises(_Stop):
                await _iterate(app, cursor=5, max_pages_per_generation=1)
        assert app.seen == [5]  # type: ignore[attr-defined]
        assert cont.call_args.args[0] == RunIn(token=6)


# =============================================================================
# Validation and context guards
# =============================================================================


class TestGuards:
    @pytest.mark.asyncio
    async def test_history_threshold_below_one_raises(self) -> None:
        app = _make_app(pages=1)
        app._context = _ctx()
        with pytest.raises(DurableIterateInvalidArgumentError) as exc:
            await _iterate(app, history_threshold=0)
        assert exc.value.code == "INVALID_INPUT_DURABLE_ITERATE_ARGUMENT"
        assert exc.value.field == "history_threshold"

    @pytest.mark.asyncio
    async def test_max_pages_below_one_raises(self) -> None:
        app = _make_app(pages=1)
        app._context = _ctx()
        with pytest.raises(DurableIterateInvalidArgumentError) as exc:
            await _iterate(app, max_pages_per_generation=0)
        assert exc.value.field == "max_pages_per_generation"

    @pytest.mark.asyncio
    async def test_raises_outside_run(self) -> None:
        app = _make_app(pages=1)  # no _context set
        with pytest.raises(AppContextError, match="iterate"):
            await _iterate(app)

    @pytest.mark.asyncio
    async def test_page_task_error_propagates(self) -> None:
        class _BoomApp(App):
            async def run(self, input: RunIn) -> RunOut:  # pragma: no cover
                return RunOut()

            async def boom(self, input: PageIn) -> PageOut:
                raise ValueError("page failed")

        app = _BoomApp()
        app._context = _ctx()
        with pytest.raises(ValueError, match="page failed"):
            await app.iterate(  # type: ignore[attr-defined]
                app.boom,
                cursor=0,
                input_factory=lambda c: PageIn(token=c or 0),
                next_cursor=lambda out: out.next_token,
                resume_input=lambda c: RunIn(token=c),
            )
