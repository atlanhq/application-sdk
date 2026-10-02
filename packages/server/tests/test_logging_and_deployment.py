"""Two process-wide defaults that were quietly wrong.

Both run at import of any server_sdk module, in a process that serves every
hosted app — so a mistake in either is a whole-host problem, not one app's.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Which branch _configure() takes depends on whether application_sdk is
# IMPORTABLE, so an ambient install silently swapped the branch under these
# tests: they passed only because this package's own venv happens not to carry
# it, and from a venv that does, all five failed. The branch they asserted is
# also the one the HOST does not take -- the consolidated image installs
# application_sdk, because automation-engine depends on it -- so the branch
# production actually runs had no level coverage at all.
#
# Pin the branch per test instead of inheriting it from the venv:
#   absent   -- sys.modules[name] = None is the documented way to make an
#               import fail; find_spec short-circuits on sys.modules and
#               answers None, so _should_own_root() sees "not installed"
#               whether or not it is.
#   present  -- a stub package first on PYTHONPATH, which find_spec answers
#               with whether or not the real one is also there.
_PIN_ABSENT = "import sys; sys.modules['application_sdk'] = None;"

_LEVEL_PROBE = (
    "{pin}"
    "import logging;"
    "from server_sdk.observability.logger_adaptor import get_logger;"
    "get_logger('server_sdk.probe');"
    "print(logging.getLevelName(logging.getLogger({logger}).level))"
)


def _run(code: str, *, extra_path: str | None = None, **env: str) -> str:
    """A fresh interpreter: logging.basicConfig configures the root once only."""
    import os

    child = {**os.environ, **env}
    for name in ("ATLAN_LOG_LEVEL", "LOG_LEVEL"):
        if name not in env:
            child.pop(name, None)
    if extra_path:
        child["PYTHONPATH"] = extra_path + os.pathsep + child.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=child
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


@pytest.fixture
def application_sdk_stub(tmp_path) -> str:
    """An importable application_sdk that is not the real one."""
    stub = tmp_path / "stub"
    (stub / "application_sdk").mkdir(parents=True)
    (stub / "application_sdk" / "__init__.py").write_text("")
    return str(stub)


@pytest.fixture(params=["absent", "present"])
def level(request, application_sdk_stub: str):
    """Read back the configured level through BOTH branches of _configure().

    They set it in different places -- basicConfig on root when this package
    owns logging, and on the ``server_sdk`` logger when it defers, because root
    then stays at WARNING and would filter INFO out before any handler ran. The
    env contract has to hold either way, and the deferred one is the host's.
    """
    if request.param == "absent":
        return lambda **env: _run(
            _LEVEL_PROBE.format(pin=_PIN_ABSENT, logger=""), **env
        )
    return lambda **env: _run(
        _LEVEL_PROBE.format(pin="", logger="'server_sdk'"),
        extra_path=application_sdk_stub,
        **env,
    )


def test_the_primary_env_name_is_read(level) -> None:
    """ATLAN_LOG_LEVEL is what the fleet sets; reading only LOG_LEVEL meant a
    tenant raising the level got no effect at all."""
    assert level(ATLAN_LOG_LEVEL="DEBUG") == "DEBUG"


def test_the_legacy_name_still_works(level) -> None:
    assert level(LOG_LEVEL="DEBUG") == "DEBUG"


def test_the_primary_wins_over_the_legacy_name(level) -> None:
    assert level(ATLAN_LOG_LEVEL="DEBUG", LOG_LEVEL="ERROR") == "DEBUG"


def test_an_unusable_level_degrades_instead_of_crashing(level) -> None:
    """basicConfig raises on an unknown level, and this runs at import of every
    server_sdk module — one typo in a chart value would crashloop the host."""
    assert level(LOG_LEVEL="nonsense") == "INFO"


def test_unset_is_info(level) -> None:
    assert level() == "INFO"


# ── the deployment half of every task queue ─────────────────────────────────


@pytest.mark.parametrize("unset_as", ["", "   "], ids=["absent", "blank"])
def test_an_unset_deployment_drops_the_prefix(monkeypatch, unset_as: str) -> None:
    """application_sdk's derive_task_queue is the single source of truth:
    app + deployment -> "atlan-{app}-{deployment}", app alone -> "{app}" BARE.

    There were two defaults here once ("default" and "local") which disagreed
    with each other; unifying them on "local" then made both disagree with the
    WORKER, which drops the prefix. Either way the submit lands on a queue
    nobody polls, reports success, and hangs.
    """
    from server_sdk.manifest import _deployment_name, worker_task_queue

    if unset_as:
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", unset_as)
    else:
        monkeypatch.delenv("ATLAN_DEPLOYMENT_NAME", raising=False)
    assert _deployment_name() == ""
    assert worker_task_queue("redshift") == "redshift"


@pytest.mark.parametrize("deployment", ["prod", "staging"])
def test_they_agree_with_the_env_set(monkeypatch, deployment: str) -> None:
    from server_sdk.manifest import _deployment_name, worker_task_queue

    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", deployment)
    assert _deployment_name() == deployment
    assert worker_task_queue("redshift") == f"atlan-redshift-{deployment}"


# ── a library must not claim the root logger ────────────────────────────────


_ROOT_PROBE = (
    "{pin}"
    "import logging;"
    "from server_sdk.observability.logger_adaptor import get_logger;"
    "get_logger('server_sdk.probe');"
    "print('HANDLERS=' + ','.join(type(h).__name__ for h in logging.getLogger().handlers))"
)


def _root_handlers(*, pin: str = "", extra_path: str | None = None) -> list[str]:
    line = _run(_ROOT_PROBE.format(pin=pin), extra_path=extra_path)
    assert line.startswith("HANDLERS="), line
    return [h for h in line.removeprefix("HANDLERS=").split(",") if h]


def test_standalone_configures_the_root_logger() -> None:
    """With nothing richer available, this package is the only thing that will
    set up logging, so it must."""
    assert _root_handlers(pin=_PIN_ABSENT) == ["StreamHandler"]


def test_it_defers_when_application_sdk_is_installed(
    application_sdk_stub: str,
) -> None:
    """basicConfig is a NO-OP once root has a handler, and in the host this
    package always imports first — so claiming root here silently disables
    application_sdk's stdlib->loguru bridge, and with it the app/deployment
    stamping, the OTLP exporter and the object-store log sink for every app.
    """
    assert _root_handlers(extra_path=application_sdk_stub) == []


def test_the_redaction_filter_is_attached_either_way() -> None:
    """Deferring root configuration must not cost the redaction."""
    from server_sdk.observability.logger_adaptor import _RedactingFilter, get_logger

    logger = get_logger("server_sdk.filter.probe")
    assert any(isinstance(f, _RedactingFilter) for f in logger.filters)


# ── the deferral window ─────────────────────────────────────────────────────


def test_the_fallback_sink_covers_the_window_then_steps_aside() -> None:
    """Deferring to application_sdk leaves a gap nothing used to cover.

    Its InterceptHandler is installed by a module-level ``basicConfig`` that
    runs only when its observability module is IMPORTED -- in the host that is
    after discovery, mount and revision logging, and on a host without
    application_sdk at all it never happens, because the serving packages
    deliberately do not depend on it. Root then has no handler and every
    server_sdk record below WARNING went to ``lastResort`` or nowhere.

    So the fallback emits only while root is unclaimed: no record dropped
    before the bridge appears, nothing double-printed after it does.
    """
    import io
    import logging

    from server_sdk.observability.logger_adaptor import _UntilRootIsClaimedHandler

    buf = io.StringIO()
    handler = _UntilRootIsClaimedHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("server_sdk.tests.window")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False

    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        root.handlers = []
        log.info("during-the-window")
        assert "during-the-window" in buf.getvalue()

        # Someone claims root, exactly as application_sdk's basicConfig would.
        root.handlers = [logging.NullHandler()]
        log.info("after-the-bridge-arrives")
        assert "after-the-bridge-arrives" not in buf.getvalue()
    finally:
        root.handlers = saved
        log.removeHandler(handler)
