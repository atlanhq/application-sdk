"""The restart marker and the bounded wait it gates."""

import asyncio
import json

import pytest

from application_sdk.common import restart_marker as rm


@pytest.fixture(autouse=True)
def marker_dir(tmp_path, monkeypatch):
    """Point the marker at a real directory, as the mounted volume would be."""
    monkeypatch.setenv(rm.MARKER_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(rm.MAX_WAIT_SECONDS_ENV, raising=False)
    return tmp_path


class _RecordingLogger:
    """Captures what the module logged, since several guards exist only to say
    something an operator needs and are otherwise behaviour-neutral."""

    def __init__(self):
        self.rows: list[tuple[str, str]] = []

    def _record(self, level):
        def log(msg, *args, **_kwargs):
            self.rows.append((level, msg % args if args else msg))

        return log

    def __getattr__(self, level):
        return self._record(level)

    def says(self, level, *fragments) -> bool:
        return any(
            lvl == level and all(f in text for f in fragments)
            for lvl, text in self.rows
        )


@pytest.fixture
def logs(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(rm, "logger", recorder)
    return recorder


@pytest.fixture
def prompt_polling(monkeypatch):
    """Shrink the release-file poll so the wait's exits are testable in real time."""
    monkeypatch.setattr(rm, "RECHECK_SECONDS", 0.02)


def test_first_start_in_a_pod_is_clean_and_leaves_a_marker(marker_dir):
    assert rm.check_and_update_the_marker() == 0
    assert json.loads((marker_dir / rm.MARKER_NAME).read_text())["starts"] == 1


def test_a_second_start_in_the_same_pod_is_a_restart(marker_dir):
    rm.check_and_update_the_marker()
    assert rm.check_and_update_the_marker() == 1
    assert rm.check_and_update_the_marker() == 2


def test_a_clean_return_makes_the_next_start_clean_again(marker_dir):
    rm.check_and_update_the_marker()
    rm.clear()
    assert not (marker_dir / rm.MARKER_NAME).exists()
    assert rm.check_and_update_the_marker() == 0


def test_clearing_is_quiet_when_there_is_no_marker(marker_dir):
    rm.clear()  # the worker may return before ever writing one


def test_no_volume_means_detection_is_inert(monkeypatch, tmp_path, logs):
    """Without the volume a marker cannot survive a restart. Detection is inert
    either way, so the only thing that distinguishes an unmounted volume from a
    healthy fleet is that it says so."""
    missing = tmp_path / "not-mounted"
    monkeypatch.setenv(rm.MARKER_DIR_ENV, str(missing))
    assert rm.check_and_update_the_marker() == 0
    assert not missing.exists(), "begin() must not create the directory"
    assert logs.says("warning", str(missing), "emptyDir"), (
        "an absent volume must be reported, or it is indistinguishable from a "
        f"pod that never restarted: {logs.rows}"
    )


def test_a_corrupt_marker_still_counts_as_a_restart(marker_dir):
    (marker_dir / rm.MARKER_NAME).write_text("{ truncated")
    assert rm.check_and_update_the_marker() == 1


def test_an_unparsable_start_count_still_counts_as_a_restart(marker_dir):
    (marker_dir / rm.MARKER_NAME).write_text(json.dumps({"starts": "many"}))
    assert rm.check_and_update_the_marker() == 1


# ---------------------------------------------------------------- the wait


async def test_a_clean_start_does_not_wait(marker_dir, monkeypatch):
    monkeypatch.setenv(rm.MAX_WAIT_SECONDS_ENV, "300")
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)


async def test_a_restart_does_not_wait_while_the_budget_is_unset(marker_dir, logs):
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)
    assert logs.says("warning", rm.MAX_WAIT_SECONDS_ENV), (
        f"must name the unset knob rather than announce a 0s wait: {logs.rows}"
    )
    assert not logs.says("warning", "not polling for up to"), (
        f"must not announce a wait it is not doing: {logs.rows}"
    )


async def test_a_restart_waits_out_the_budget_then_proceeds(
    marker_dir, monkeypatch, prompt_polling
):
    rm.check_and_update_the_marker()
    monkeypatch.setenv(rm.MAX_WAIT_SECONDS_ENV, "1")
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=5)
    assert loop.time() - started >= 1, "returned before the budget was spent"


async def test_the_wait_holds_until_something_ends_it(
    marker_dir, monkeypatch, prompt_polling
):
    """The point of the wait: with a long budget and nothing to release it, the
    worker is still not polling."""
    rm.check_and_update_the_marker()
    monkeypatch.setenv(rm.MAX_WAIT_SECONDS_ENV, "300")
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(asyncio.Event()))
    await asyncio.sleep(0.1)
    assert not task.done(), "the wait returned without being released"
    task.cancel()


async def test_the_release_file_ends_the_wait_early(
    marker_dir, monkeypatch, prompt_polling
):
    rm.check_and_update_the_marker()
    monkeypatch.setenv(rm.MAX_WAIT_SECONDS_ENV, "300")
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(asyncio.Event()))
    await asyncio.sleep(0.05)
    assert not task.done()
    (marker_dir / rm.RELEASE_NAME).write_text("")
    await asyncio.wait_for(task, timeout=5)


async def test_shutdown_ends_the_wait(marker_dir, monkeypatch, prompt_polling):
    rm.check_and_update_the_marker()
    monkeypatch.setenv(rm.MAX_WAIT_SECONDS_ENV, "300")
    shutdown = asyncio.Event()
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(shutdown))
    await asyncio.sleep(0.05)
    assert not task.done()
    shutdown.set()
    await asyncio.wait_for(task, timeout=5)


async def test_a_failure_setting_up_the_wait_starts_the_worker_anyway(
    marker_dir, monkeypatch
):
    rm.check_and_update_the_marker()
    monkeypatch.setenv(rm.MAX_WAIT_SECONDS_ENV, "300")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("no clock")

    monkeypatch.setattr(rm, "wait_for_pod_to_get_replaced", boom)
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)
