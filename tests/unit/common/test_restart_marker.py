"""The restart marker and the bounded wait it gates."""

import asyncio
import json

import httpx
import pytest

from application_sdk.common import restart_marker as rm


@pytest.fixture(autouse=True)
def marker_dir(tmp_path, monkeypatch):
    """Point the marker at a real directory, as the mounted volume would be."""
    monkeypatch.setenv(rm.MARKER_DIR_ENV, str(tmp_path))
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
    assert (
        not missing.exists()
    ), "check_and_update_the_marker() must not create the directory"
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


def test_a_non_utf8_marker_counts_as_a_restart_and_is_replaced(marker_dir):
    """A marker that cannot be decoded must not reach the caller: it is read
    before the wait's own error handling, so raising here would stop the worker
    from starting at all."""
    path = marker_dir / rm.MARKER_NAME
    path.write_bytes(b"\xff\xfe not utf-8")
    assert rm.check_and_update_the_marker() == 1
    # replaced, so the next start reads a usable marker rather than tripping again
    assert json.loads(path.read_text())["starts"] == 2


# ---------------------------------------------------------------- the wait


async def test_a_clean_start_does_not_wait(marker_dir, monkeypatch):
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)


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
    marker_dir, monkeypatch, prompt_polling
):
    rm.check_and_update_the_marker()
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 1)
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
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(asyncio.Event()))
    await asyncio.sleep(0.1)
    assert not task.done(), "the wait returned without being released"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_release_file_ends_the_wait_early(
    marker_dir, monkeypatch, prompt_polling
):
    rm.check_and_update_the_marker()
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(asyncio.Event()))
    await asyncio.sleep(0.05)
    assert not task.done()
    (marker_dir / rm.RELEASE_NAME).write_text("")
    await asyncio.wait_for(task, timeout=5)


async def test_shutdown_ends_the_wait(marker_dir, monkeypatch, prompt_polling):
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


# ------------------------------------------------- why the container restarted


class _Apiserver:
    """A stand-in for the in-cluster apiserver that records every call.

    Recording is the point as much as answering: the cost of asking why a
    container restarted is exactly the number of these calls, and a test that
    only checked the decision would not notice a second read creeping in.
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.tokens: list[str | None] = []
        self.client_kwargs: list[dict] = []
        self.pod: dict = {}
        self.refuse: dict[str, int] = {}

    def respond(self, method: str, url: str, headers: dict) -> httpx.Response:
        self.calls.append((method, url))
        self.tokens.append(headers.get("Authorization"))
        return httpx.Response(
            self.refuse.get(method, 200),
            json=self.pod,
            request=httpx.Request(method, url),
        )

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


class _FakeClient:
    def __init__(self, server: _Apiserver, **kwargs):
        self._server = server
        server.client_kwargs.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def request(self, method, url, headers=None):
        return self._server.respond(method, str(url), headers or {})


def _pod(*containers: tuple[str, str | None]) -> dict:
    """A pod status carrying one container status per ``(name, reason)``, where a
    reason of ``None`` is a container with no earlier termination recorded."""
    return {
        "status": {
            "containerStatuses": [
                {
                    "name": name,
                    "lastState": ({"terminated": {"reason": reason}} if reason else {}),
                }
                for name, reason in containers
            ]
        }
    }


@pytest.fixture
def apiserver(monkeypatch, tmp_path):
    """The mounted service account and an apiserver that answers about this pod."""
    account = tmp_path / "serviceaccount"
    account.mkdir()
    (account / "token").write_text("this-pods-token\n")
    (account / "namespace").write_text("oomtest\n")
    (account / "ca.crt").write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setattr(rm, "SERVICE_ACCOUNT_DIR", account)
    monkeypatch.setenv(rm.POD_NAME_ENV, "probe-worker-7f9")
    monkeypatch.delenv(rm.CONTAINER_NAME_ENV, raising=False)
    monkeypatch.setattr(rm, "OOM_RESTART_CHECK", rm.CHECK_API)
    monkeypatch.setattr(rm, "OOM_RESTART_ACTION", "park")
    server = _Apiserver()
    monkeypatch.setattr(rm.httpx, "AsyncClient", lambda **kw: _FakeClient(server, **kw))
    return server


async def _still_idle(coro, seconds: float = 0.15):
    """Run the restart handling, and assert it has not resumed polling.

    Returns the task so a test can go on to release, cancel or await it.
    """
    task = asyncio.ensure_future(coro)
    await asyncio.sleep(seconds)
    assert not task.done(), "this worker resumed polling instead of idling"
    return task


async def _cancel(task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_every_start_says_which_handling_it_is_running(marker_dir, logs):
    """Two deployments of one image differ only in this configuration, so a pod's
    own log is the only place to read back which one it got."""
    asyncio.run(rm.wait_if_pod_restarted(asyncio.Event()))
    assert logs.says(
        "info", "restart handling:", "check=", "action="
    ), f"a pod must say which restart handling it is running: {logs.rows}"


async def test_without_the_api_check_a_restart_idles_without_asking_why(
    marker_dir, monkeypatch, apiserver, prompt_polling
):
    """The cheap topology: no apiserver call at all, so no RBAC and no per-pod
    cost - and a restart of any cause is handled as if memory caused it."""
    monkeypatch.setattr(rm, "OOM_RESTART_CHECK", "none")
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    rm.check_and_update_the_marker()
    task = await _still_idle(rm.wait_if_pod_restarted(asyncio.Event()))
    assert (
        apiserver.calls == []
    ), f"the none check must not call the apiserver: {apiserver.calls}"
    await _cancel(task)


async def test_an_out_of_memory_restart_idles_under_park(
    marker_dir, monkeypatch, apiserver, prompt_polling
):
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    rm.check_and_update_the_marker()
    task = await _still_idle(rm.wait_if_pod_restarted(asyncio.Event()))
    assert apiserver.methods() == [
        "GET"
    ], f"one restart must cost one read, and park must not delete: {apiserver.calls}"
    await _cancel(task)


async def test_a_restart_that_was_not_out_of_memory_polls_immediately(
    marker_dir, monkeypatch, apiserver, prompt_polling, logs
):
    """R3: a container that merely crashed comes back on a limit that was never
    the problem, so making it wait costs availability for nothing."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", "Error"))
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)
    assert not logs.says(
        "warning", "not polling for up to"
    ), f"a non-memory restart must not be made to wait: {logs.rows}"
    assert apiserver.methods() == ["GET"]


async def test_a_refused_read_idles_rather_than_resuming(
    marker_dir, monkeypatch, apiserver, prompt_polling, logs
):
    """Fail closed. "We could not find out, so we assume it is fine" puts the work
    straight back onto a pod that cannot hold it."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.refuse["GET"] = 403
    rm.check_and_update_the_marker()
    task = await _still_idle(rm.wait_if_pod_restarted(asyncio.Event()))
    assert logs.says(
        "warning", "could not read why the earlier container"
    ), f"a refused read must say so, not pass for a clean answer: {logs.rows}"
    await _cancel(task)


async def test_a_pod_that_cannot_name_itself_idles(
    marker_dir, monkeypatch, apiserver, prompt_polling
):
    """Without the downward API the read cannot even be addressed. That is a
    deployment fault, and guessing a pod name would read someone else's."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    monkeypatch.delenv(rm.POD_NAME_ENV, raising=False)
    rm.check_and_update_the_marker()
    task = await _still_idle(rm.wait_if_pod_restarted(asyncio.Event()))
    assert apiserver.calls == []
    await _cancel(task)


async def test_a_status_with_no_earlier_termination_idles(
    marker_dir, monkeypatch, apiserver, prompt_polling
):
    """The marker says a container already ran here, so a status that carries no
    termination is an unanswered question, not a clean bill of health."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", None))
    rm.check_and_update_the_marker()
    task = await _still_idle(rm.wait_if_pod_restarted(asyncio.Event()))
    await _cancel(task)


async def test_a_sidecars_kill_is_not_read_as_this_containers(
    marker_dir, monkeypatch, apiserver, prompt_polling, logs
):
    """A sidecar that ran out of memory says nothing about the worker's limit, and
    replacing the pod over it would replace a pod that was sized fine."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    monkeypatch.setenv(rm.CONTAINER_NAME_ENV, "worker")
    apiserver.pod = _pod(("daemon", rm.OOM_REASON), ("worker", "Error"))
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)
    assert not logs.says(
        "warning", "not polling for up to"
    ), f"the sidecar's reason was read as this container's: {logs.rows}"


async def test_a_status_named_for_another_container_idles(
    marker_dir, monkeypatch, apiserver, prompt_polling, logs
):
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    monkeypatch.setenv(rm.CONTAINER_NAME_ENV, "worker")
    apiserver.pod = _pod(("renamed-worker", "Error"))
    rm.check_and_update_the_marker()
    task = await _still_idle(rm.wait_if_pod_restarted(asyncio.Event()))
    assert logs.says("warning", "has no container named worker")
    await _cancel(task)


async def test_a_multi_container_pod_without_the_container_name_idles(
    marker_dir, monkeypatch, apiserver, prompt_polling, logs
):
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", "Error"), ("daemon", "Error"))
    rm.check_and_update_the_marker()
    task = await _still_idle(rm.wait_if_pod_restarted(asyncio.Event()))
    assert logs.says(
        "warning", rm.CONTAINER_NAME_ENV, "2 containers"
    ), f"an unidentifiable container must say so: {logs.rows}"
    await _cancel(task)


async def test_a_single_container_pod_needs_no_container_name(
    marker_dir, monkeypatch, apiserver, prompt_polling, logs
):
    """The one case where which container this is has no ambiguity to resolve."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("anything", "Error"))
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)
    assert not logs.says("warning", "not polling for up to")


async def test_the_read_is_this_pod_authenticated_with_its_mounted_token(apiserver):
    """The URL is built from this pod's own name, which is what keeps the grant a
    Role over one namespace rather than a cluster-wide read."""
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    assert await rm.last_termination_reason() == rm.OOM_REASON
    method, url = apiserver.calls[0]
    assert method == "GET"
    assert url == (
        "https://kubernetes.default.svc/api/v1/namespaces/oomtest/pods/probe-worker-7f9"
    )
    assert apiserver.tokens == [
        "Bearer this-pods-token"
    ], "the call must carry the pod's own mounted token"
    assert apiserver.client_kwargs[0]["verify"].endswith(
        "/ca.crt"
    ), "the apiserver must be verified against the mounted CA"


# ----------------------------------------------------------- deleting this pod


@pytest.fixture
def delete_action(monkeypatch):
    monkeypatch.setattr(rm, "OOM_RESTART_ACTION", rm.ACTION_DELETE)
    monkeypatch.setattr(rm, "OOM_RESTART_SETTLE_SECONDS", 0.3)


async def test_the_delete_waits_out_the_settle_window(
    marker_dir, monkeypatch, apiserver, prompt_polling, delete_action
):
    """A pod's size is fixed when it is admitted and the recommendation rises only
    after the kill, so deleting immediately buys another pod of the size that
    just died."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    rm.check_and_update_the_marker()
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(asyncio.Event()))
    await asyncio.sleep(0.1)
    assert apiserver.methods() == [
        "GET"
    ], f"deleted before the settle window elapsed: {apiserver.calls}"
    await asyncio.sleep(0.4)
    assert apiserver.methods() == [
        "GET",
        "DELETE",
    ], f"the settle window elapsed and nothing was deleted: {apiserver.calls}"
    assert (
        apiserver.calls[1][1] == apiserver.calls[0][1]
    ), "the delete must address the same pod the read did"
    assert (
        not task.done()
    ), "polling resumed instead of waiting for the shutdown the delete brings"
    await _cancel(task)


async def test_nothing_is_deleted_when_the_pod_is_already_being_replaced(
    marker_dir, monkeypatch, apiserver, prompt_polling, delete_action
):
    """A shutdown during the settle window means something else owns this pod, and
    deleting on top of it would take out the replacement instead."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    rm.check_and_update_the_marker()
    shutdown = asyncio.Event()
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(shutdown))
    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1)
    assert apiserver.methods() == [
        "GET"
    ], f"deleted a pod that was already being replaced: {apiserver.calls}"


async def test_a_refused_delete_idles_out_the_budget_instead_of_polling(
    marker_dir, monkeypatch, apiserver, prompt_polling, delete_action, logs
):
    """A service account without the verb is the expected refusal. Resuming on it
    would be a worker polling on the limit that already killed it, having
    reported that it handled the kill."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.refuse["DELETE"] = 403
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    rm.check_and_update_the_marker()
    task = await _still_idle(rm.wait_if_pod_restarted(asyncio.Event()), seconds=0.5)
    assert apiserver.methods() == ["GET", "DELETE"]
    assert logs.says(
        "warning", "could not get this pod replaced"
    ), f"a refused delete must say so rather than read as done: {logs.rows}"
    await _cancel(task)


async def test_the_settle_window_cannot_outlast_the_budget(
    marker_dir, monkeypatch, apiserver, prompt_polling, delete_action
):
    """Both phases come out of the one budget, so deleting can never idle a worker
    for longer than parking would."""
    monkeypatch.setattr(rm, "OOM_RESTART_SETTLE_SECONDS", 300)
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 1)
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    rm.check_and_update_the_marker()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=5)
    assert loop.time() - started < 3, "the settle window outlasted the budget"
    assert apiserver.methods() == ["GET", "DELETE"]


async def test_a_restart_that_was_not_out_of_memory_is_never_deleted(
    marker_dir, monkeypatch, apiserver, prompt_polling, delete_action
):
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", "Error"))
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)
    assert apiserver.methods() == [
        "GET"
    ], f"deleted a pod over a restart memory did not cause: {apiserver.calls}"


# ------------------------------------------------------------ ejecting this pod


@pytest.fixture
def eject_action(monkeypatch, tmp_path):
    """The disk-backed volume the node watches, and the action that overflows it."""
    volume = tmp_path / "eject"
    volume.mkdir()
    monkeypatch.setenv(rm.EJECT_DIR_ENV, str(volume))
    monkeypatch.setattr(rm, "OOM_RESTART_ACTION", rm.ACTION_EJECT)
    monkeypatch.setattr(rm, "OOM_RESTART_SETTLE_SECONDS", 0.3)
    return volume


async def test_the_eject_waits_out_the_settle_window(
    marker_dir, monkeypatch, apiserver, prompt_polling, eject_action
):
    """Same reason the delete waits: a pod admitted before the recommendation
    rises is another pod of the size that just died."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    rm.check_and_update_the_marker()
    ballast = eject_action / rm.EJECT_NAME
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(asyncio.Event()))
    await asyncio.sleep(0.1)
    assert not ballast.exists(), "overflowed the volume before the settle window"
    await asyncio.sleep(0.4)
    assert (
        ballast.stat().st_size == rm.EJECT_BYTES
    ), "the settle window elapsed and the volume was not overflowed"
    assert (
        not task.done()
    ), "polling resumed instead of waiting for the eviction the overflow brings"
    await _cancel(task)


async def test_the_eject_asks_nothing_of_the_apiserver(
    marker_dir, monkeypatch, apiserver, prompt_polling, eject_action
):
    """The whole point of this arm: the replacement costs no write verb anywhere.
    Only the read that established the cause is spent."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    rm.check_and_update_the_marker()
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(asyncio.Event()))
    await asyncio.sleep(0.5)
    assert apiserver.methods() == [
        "GET"
    ], f"the eject must not call the apiserver to replace the pod: {apiserver.calls}"
    await _cancel(task)


async def test_nothing_is_ejected_when_the_pod_is_already_being_replaced(
    marker_dir, monkeypatch, apiserver, prompt_polling, eject_action
):
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    rm.check_and_update_the_marker()
    shutdown = asyncio.Event()
    task = asyncio.ensure_future(rm.wait_if_pod_restarted(shutdown))
    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1)
    assert not (
        eject_action / rm.EJECT_NAME
    ).exists(), "overflowed the volume of a pod that was already being replaced"


async def test_a_missing_eject_volume_idles_instead_of_polling(
    marker_dir, monkeypatch, apiserver, prompt_polling, eject_action, logs
):
    """Without the volume there is nothing of this pod's own to overflow. Writing
    to the container filesystem instead would charge the node's disk and get
    somebody else's pod evicted, so this arm is inert without its volume - and
    says so, because otherwise it is indistinguishable from one that worked."""
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    monkeypatch.setenv(rm.EJECT_DIR_ENV, str(eject_action / "not-mounted"))
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    rm.check_and_update_the_marker()
    task = await _still_idle(rm.wait_if_pod_restarted(asyncio.Event()), seconds=0.5)
    assert logs.says(
        "warning", "not-mounted", "sizeLimit"
    ), f"an absent eject volume must be reported: {logs.rows}"
    assert logs.says("warning", "could not get this pod replaced")
    await _cancel(task)


async def test_an_unwritable_eject_volume_idles_instead_of_polling(
    marker_dir, monkeypatch, apiserver, prompt_polling, eject_action, logs
):
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    eject_action.chmod(0o500)
    apiserver.pod = _pod(("worker", rm.OOM_REASON))
    rm.check_and_update_the_marker()
    try:
        task = await _still_idle(rm.wait_if_pod_restarted(asyncio.Event()), seconds=0.5)
        assert logs.says(
            "warning", "could not write"
        ), f"a failed overflow must say so: {logs.rows}"
        assert logs.says(
            "warning", "could not get this pod replaced"
        ), f"a write that did not happen must not read as a pod on its way out: {logs.rows}"
        await _cancel(task)
    finally:
        eject_action.chmod(0o700)


async def test_a_restart_that_was_not_out_of_memory_is_never_ejected(
    marker_dir, monkeypatch, apiserver, prompt_polling, eject_action
):
    monkeypatch.setattr(rm, "DIRTY_RESTART_IDLE_MAX_SECONDS", 300)
    apiserver.pod = _pod(("worker", "Error"))
    rm.check_and_update_the_marker()
    await asyncio.wait_for(rm.wait_if_pod_restarted(asyncio.Event()), timeout=1)
    assert not (
        eject_action / rm.EJECT_NAME
    ).exists(), "ejected a pod over a restart memory did not cause"
