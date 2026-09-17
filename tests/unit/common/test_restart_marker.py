"""The restart marker and the bounded wait it gates."""

import asyncio

import pytest

from application_sdk.common import restart_marker as rm


@pytest.fixture(autouse=True)
def marker_dir(tmp_path, monkeypatch):
    """Point the marker at a real directory, as the mounted volume would be."""
    monkeypatch.setattr(rm, "MARKER_DIR", tmp_path)
    # The shipped default is a positive number; switch it off unless a test asks.
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 0)
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


@pytest.fixture
def told_to_wait(monkeypatch):
    """Stand in for an endpoint that says a replacement is on its way, so the
    wait's own exits can be tested without one."""

    async def answer():
        return (True, "eviction-scheduled", 0)

    monkeypatch.setattr(rm, "OOM_RESTART_CHECK", rm.CHECK_API)
    monkeypatch.setattr(rm, "ask_what_this_restart_earns", answer)


def test_first_start_in_a_pod_is_clean_and_leaves_a_marker(marker_dir):
    assert rm.check_and_update_the_marker() == 0
    assert (marker_dir / rm.MARKER_NAME).read_text() == "1"


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
    monkeypatch.setattr(rm, "MARKER_DIR", missing)
    assert rm.check_and_update_the_marker() == 0
    assert (
        not missing.exists()
    ), "check_and_update_the_marker() must not create the directory"
    assert logs.says("debug", str(missing), "emptyDir"), (
        "an absent volume must still be reported, at debug because most of the "
        f"fleet does not mount it: {logs.rows}"
    )


def test_an_unreadable_marker_still_counts_as_a_restart(marker_dir):
    """The file existing is the signal; only the count is lost, and one is the
    answer that changes behaviour."""
    (marker_dir / rm.MARKER_NAME).write_text("not a number")
    assert rm.check_and_update_the_marker() == 1


def test_a_non_utf8_marker_counts_as_a_restart_and_is_replaced(marker_dir):
    """A marker that cannot be decoded must not reach the caller: it is read
    before the wait's own error handling, so raising here would stop the worker
    from starting at all."""
    path = marker_dir / rm.MARKER_NAME
    path.write_bytes(b"\xff\xfe not utf-8")
    assert rm.check_and_update_the_marker() == 1
    # replaced, so the next start reads a usable marker rather than tripping again
    assert path.read_text() == "2"


# ---------------------------------------------------------------- the wait


async def test_a_clean_start_does_not_wait(marker_dir, monkeypatch):
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)


async def test_only_the_first_restart_in_a_pod_waits(marker_dir, monkeypatch, logs):
    """A second restart means the first wait did not get the pod replaced, so
    waiting again would pay the same penalty for the same non-answer."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    rm.check_and_update_the_marker()  # start 1 -> marker says 1 restart
    rm.check_and_update_the_marker()  # start 2 -> marker says 2
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)
    assert logs.says(
        "warning", "polls instead of waiting again"
    ), f"a second restart must say why it is not waiting: {logs.rows}"
    assert not logs.says(
        "warning", "not polling for up to"
    ), f"it must not announce a wait it is not doing: {logs.rows}"


async def test_a_restart_does_not_wait_while_waiting_is_switched_off(marker_dir, logs):
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)
    assert logs.says(
        "warning", "ATLAN_DIRTY_RESTART_IDLE_MAX_SECONDS=0"
    ), f"must say it is switched off rather than announce a 0s wait: {logs.rows}"
    assert not logs.says(
        "warning", "not polling for up to"
    ), f"must not announce a wait it is not doing: {logs.rows}"


async def test_a_restart_waits_out_the_budget_then_proceeds(
    marker_dir, monkeypatch, prompt_polling, told_to_wait
):
    rm.check_and_update_the_marker()
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=5)
    assert loop.time() - started >= 1, "returned before the budget was spent"


async def test_the_wait_holds_until_something_ends_it(
    marker_dir, monkeypatch, prompt_polling, told_to_wait
):
    """The point of the wait: with a long budget and nothing to release it, the
    worker is still not polling."""
    rm.check_and_update_the_marker()
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(asyncio.Event()))
    await asyncio.sleep(0.1)
    assert not task.done(), "the wait returned without being released"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_shutdown_ends_the_wait(
    marker_dir, monkeypatch, prompt_polling, told_to_wait
):
    rm.check_and_update_the_marker()
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
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
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("no clock")

    monkeypatch.setattr(rm, "wait_for_pod_to_get_replaced", boom)
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)


# -------------------------------------------------- what this restart earns


@pytest.fixture
def advice(monkeypatch, tmp_path):
    """Everything the ask needs, plus a place to put the answer and a record of
    what was asked."""
    (tmp_path / "namespace").write_text("athena-app")
    monkeypatch.setattr(rm, "SERVICE_ACCOUNT_DIR", tmp_path)
    monkeypatch.setenv(rm.ADVICE_URL_ENV, "http://rerouter/restart-advice")
    monkeypatch.setenv("K8S_POD_NAME", "athena-worker-1")
    monkeypatch.setattr(rm, "APPLICATION_NAME", "athena")
    monkeypatch.setattr(rm, "OOM_RESTART_CHECK", rm.CHECK_API)

    state: dict = {
        "asked": [],
        "body": {"wait": False, "reason": "unset"},
        "raise": None,
    }

    class _Response:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class _Client:
        def __init__(self, *_a, **kw):
            state["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url, params=None):
            state["asked"].append((url, params))
            if state["raise"] is not None:
                raise state["raise"]
            return _Response(state["body"])

    monkeypatch.setattr(rm.httpx, "AsyncClient", _Client)
    return state


async def test_a_replacement_on_its_way_holds_the_worker_back(
    marker_dir, monkeypatch, advice, prompt_polling
):
    advice["body"] = {"wait": True, "reason": "eviction-scheduled", "waitSeconds": 1}
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    rm.check_and_update_the_marker()

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=5)
    assert loop.time() - started >= 1, "a scheduled eviction must be waited for"


async def test_the_ask_names_this_pod_and_nothing_else(
    marker_dir, monkeypatch, advice, prompt_polling
):
    advice["body"] = {"wait": False, "reason": "no-eviction-scheduled"}
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=2)

    assert len(advice["asked"]) == 1, "exactly one call, on the restart path only"
    url, params = advice["asked"][0]
    assert url == "http://rerouter/restart-advice"
    assert params == {
        "namespace": "athena-app",
        "pod": "athena-worker-1",
        "container": "athena",
    }
    assert advice["timeout"] == rm.ADVICE_TIMEOUT_SECONDS


async def test_nothing_coming_means_the_worker_polls_now(
    marker_dir, monkeypatch, advice, logs, prompt_polling
):
    advice["body"] = {"wait": False, "reason": "not-vpa-managed"}
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    rm.check_and_update_the_marker()

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=2)
    assert loop.time() - started < 1, "a worker nothing is coming for must not wait"
    assert logs.says(
        "warning", "not-vpa-managed"
    ), f"the operator has to be told why it did not wait: {logs.rows}"


async def test_an_unreachable_endpoint_polls_rather_than_waiting(
    marker_dir, monkeypatch, advice, logs, prompt_polling
):
    """Fail open. A worker that cannot ask is in the same position it was in
    before any of this existed, and waiting on a silent service would spend the
    activity's retries for nothing."""
    advice["raise"] = RuntimeError("connection refused")
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    rm.check_and_update_the_marker()

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=2)
    assert loop.time() - started < 1, "an unreachable endpoint must not hold a worker"
    assert logs.says("warning", "could not find out what this restart earns"), logs.rows


async def test_an_unparsable_answer_polls_rather_than_waiting(
    marker_dir, monkeypatch, advice, prompt_polling
):
    advice["body"] = ["not", "an", "object"]
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    rm.check_and_update_the_marker()

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=2)
    assert (
        loop.time() - started < 1
    ), "a body that will not parse must not hold a worker"


async def test_the_budget_bounds_a_long_answer(
    marker_dir, monkeypatch, advice, prompt_polling
):
    """The endpoint's number is advisory. A wait longer than the budget would
    outlive the retries it exists to protect."""
    advice["body"] = {"wait": True, "reason": "eviction-scheduled", "waitSeconds": 9999}
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 1)
    rm.check_and_update_the_marker()

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=5)
    assert 1 <= loop.time() - started < 4, "the wait must be clamped to the budget"


async def test_without_the_check_nothing_is_asked(
    marker_dir, monkeypatch, advice, prompt_polling
):
    monkeypatch.setattr(rm, "OOM_RESTART_CHECK", "none")
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=2)
    assert advice["asked"] == [], "the check being off must cost no call at all"


async def test_hostname_names_the_pod_when_the_variable_is_unset(
    marker_dir, monkeypatch, advice, prompt_polling
):
    """The kubelet sets HOSTNAME to the pod name for every pod, so a deployment
    that never wired K8S_POD_NAME can still ask about itself. Same fallback the
    OTel attributes and the sizing interceptor use."""
    monkeypatch.delenv("K8S_POD_NAME", raising=False)
    monkeypatch.setenv("HOSTNAME", "athena-worker-9")
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=2)
    assert advice["asked"], "HOSTNAME alone must be enough to name this pod"
    _, params = advice["asked"][0]
    assert params["pod"] == "athena-worker-9"


async def test_a_pod_that_cannot_name_itself_asks_nothing(
    marker_dir, monkeypatch, advice, prompt_polling
):
    # Both sources of the pod's own name, since HOSTNAME backs the explicit one.
    monkeypatch.delenv("K8S_POD_NAME", raising=False)
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=2)
    assert advice["asked"] == [], "without its own name it cannot ask about itself"
