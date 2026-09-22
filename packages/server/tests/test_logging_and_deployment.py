"""Two process-wide defaults that were quietly wrong.

Both run at import of any server_sdk module, in a process that serves every
hosted app — so a mistake in either is a whole-host problem, not one app's.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_PROBE = (
    "import logging;"
    "from server_sdk.observability.logger_adaptor import get_logger;"
    "get_logger('x');"
    "print(logging.getLevelName(logging.getLogger().level))"
)


def _root_level(**env: str) -> str:
    """A fresh interpreter: logging.basicConfig configures the root once only."""
    import os

    child = {**os.environ, **env}
    for name in ("ATLAN_LOG_LEVEL", "LOG_LEVEL"):
        if name not in env:
            child.pop(name, None)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, env=child
    )
    return out.stdout.strip().splitlines()[-1]


def test_the_primary_env_name_is_read() -> None:
    """ATLAN_LOG_LEVEL is what the fleet sets; reading only LOG_LEVEL meant a
    tenant raising the level got no effect at all."""
    assert _root_level(ATLAN_LOG_LEVEL="DEBUG") == "DEBUG"


def test_the_legacy_name_still_works() -> None:
    assert _root_level(LOG_LEVEL="DEBUG") == "DEBUG"


def test_the_primary_wins_over_the_legacy_name() -> None:
    assert _root_level(ATLAN_LOG_LEVEL="DEBUG", LOG_LEVEL="ERROR") == "DEBUG"


def test_an_unusable_level_degrades_instead_of_crashing() -> None:
    """basicConfig raises on an unknown level, and this runs at import of every
    server_sdk module — one typo in a chart value would crashloop the host."""
    assert _root_level(LOG_LEVEL="nonsense") == "INFO"


def test_unset_is_info() -> None:
    assert _root_level() == "INFO"


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
    "import logging;"
    "from server_sdk.observability.logger_adaptor import get_logger;"
    "get_logger('x');"
    "print('HANDLERS=' + ','.join(type(h).__name__ for h in logging.getLogger().handlers))"
)


def _root_handlers(extra_path: str | None = None) -> list[str]:
    import os
    import subprocess

    env = dict(os.environ)
    if extra_path:
        env["PYTHONPATH"] = extra_path + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", _ROOT_PROBE], capture_output=True, text=True, env=env
    )
    line = next(
        (ln for ln in out.stdout.splitlines() if ln.startswith("HANDLERS=")),
        "HANDLERS=",
    )
    return [h for h in line.removeprefix("HANDLERS=").split(",") if h]


def test_standalone_configures_the_root_logger() -> None:
    """With nothing richer available, this package is the only thing that will
    set up logging, so it must."""
    assert _root_handlers() == ["StreamHandler"]


def test_it_defers_when_application_sdk_is_installed(tmp_path) -> None:
    """basicConfig is a NO-OP once root has a handler, and in the host this
    package always imports first — so claiming root here silently disables
    application_sdk's stdlib->loguru bridge, and with it the app/deployment
    stamping, the OTLP exporter and the object-store log sink for every app.
    """
    stub = tmp_path / "stub"
    (stub / "application_sdk").mkdir(parents=True)
    (stub / "application_sdk" / "__init__.py").write_text("")
    assert _root_handlers(str(stub)) == []


def test_the_redaction_filter_is_attached_either_way() -> None:
    """Deferring root configuration must not cost the redaction."""
    from server_sdk.observability.logger_adaptor import _RedactingFilter, get_logger

    logger = get_logger("server_sdk.filter.probe")
    assert any(isinstance(f, _RedactingFilter) for f in logger.filters)
