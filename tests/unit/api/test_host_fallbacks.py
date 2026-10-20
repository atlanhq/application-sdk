"""The host's fallbacks are loud: logged, and taken only for what they are for.

* ``asgi._package_generated_dir`` serves no metadata routes only when the app
  package itself is not installed, and says so; a package that exists but fails
  to import raises.
* ``_logging.get_logger`` on an api-only install gives the ``application_sdk``
  stdlib logger a minimal INFO handler (stderr) so the host's INFO lines are not
  dropped, without touching a logging setup the host already has. The full SDK
  install never takes that path.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from application_sdk import _install, _logging
from application_sdk.handler import asgi

# --- asgi._package_generated_dir -------------------------------------------------


def _write_package(root: Path, name: str, body: str) -> None:
    pkg = root / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text(body)


@pytest.fixture
def warn(monkeypatch: pytest.MonkeyPatch) -> mock.Mock:
    logger = mock.Mock()
    monkeypatch.setattr(asgi, "logger", logger)
    return logger


@pytest.mark.parametrize("package", ["no_such_app_pkg", "no_such_app_pkg.sub"])
def test_a_missing_app_package_serves_no_metadata_routes_and_says_so(
    package: str, warn: mock.Mock
) -> None:
    assert asgi._package_generated_dir(package) is None
    warn.warning.assert_called_once()
    args = warn.warning.call_args.args
    assert package in args and "no_such_app_pkg" in args


def test_a_package_whose_dependency_is_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warn: mock.Mock
) -> None:
    _write_package(tmp_path, "broken_dep_app", "import some_missing_dependency_xyz\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "broken_dep_app", raising=False)

    with pytest.raises(ModuleNotFoundError) as raised:
        asgi._package_generated_dir("broken_dep_app")
    assert raised.value.name == "some_missing_dependency_xyz"
    warn.warning.assert_not_called()


def test_a_package_that_fails_to_import_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warn: mock.Mock
) -> None:
    _write_package(
        tmp_path, "broken_code_app", "from os import no_such_name_in_os  # noqa\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "broken_code_app", raising=False)

    with pytest.raises(ImportError):
        asgi._package_generated_dir("broken_code_app")
    warn.warning.assert_not_called()


def test_an_installed_package_with_a_generated_folder_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_package(tmp_path, "good_app_pkg", "")
    (tmp_path / "good_app_pkg" / "generated").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "good_app_pkg", raising=False)

    found = asgi._package_generated_dir("good_app_pkg")
    assert found is not None and found.name == "generated"


# --- _logging on an api-only install ----------------------------------------------


def _no_root_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty the root logger's handlers for the rest of the test.

    Called from the test body: pytest attaches its capture handlers to the root
    logger for the call phase, after fixtures have run.
    """
    monkeypatch.setattr(logging.getLogger(), "handlers", [])


@pytest.fixture
def clean_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[logging.Logger]:
    """No handler on the ``application_sdk`` logger, restored afterwards."""
    sdk_logger = logging.getLogger("application_sdk")
    level = sdk_logger.level
    monkeypatch.setattr(sdk_logger, "handlers", [])
    sdk_logger.setLevel(logging.NOTSET)
    _logging._configure_api_only_logging.cache_clear()
    yield sdk_logger
    _logging._configure_api_only_logging.cache_clear()
    sdk_logger.setLevel(level)  # also clears the loggers' isEnabledFor cache


@pytest.fixture
def api_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the API host: no worker SDK, so no structured logger."""
    monkeypatch.setattr(_install, "api_only_install", lambda: True)
    monkeypatch.setitem(
        sys.modules, "application_sdk.observability.logger_adaptor", None
    )


@pytest.mark.usefixtures("api_only")
def test_api_only_info_lines_reach_stderr(
    clean_logging: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_root_handlers(monkeypatch)
    logger = _logging.get_logger("application_sdk.handler.routes")
    assert isinstance(logger, logging.Logger)
    logger.info("preflight verdict %s", "READY")

    (handler,) = clean_logging.handlers
    assert isinstance(handler, _logging._ApiOnlyHandler)
    assert handler.level == logging.INFO
    assert clean_logging.level == logging.INFO
    err = capsys.readouterr().err
    assert "INFO application_sdk.handler.routes - preflight verdict READY" in err


@pytest.mark.usefixtures("api_only")
def test_api_only_handler_is_installed_once(
    clean_logging: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_root_handlers(monkeypatch)
    _logging.get_logger("application_sdk.a")
    _logging.get_logger("application_sdk.b")
    assert len(clean_logging.handlers) == 1


@pytest.mark.usefixtures("api_only")
def test_an_existing_root_handler_is_left_alone(
    clean_logging: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_handler = logging.NullHandler()
    monkeypatch.setattr(logging.getLogger(), "handlers", [root_handler])
    _logging.get_logger("application_sdk.a")
    assert clean_logging.handlers == []
    assert clean_logging.level == logging.NOTSET


@pytest.mark.usefixtures("api_only")
def test_an_existing_sdk_handler_is_left_alone(
    clean_logging: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_root_handlers(monkeypatch)
    own = logging.NullHandler()
    clean_logging.handlers.append(own)
    _logging.get_logger("application_sdk.a")
    assert clean_logging.handlers == [own]
    assert clean_logging.level == logging.NOTSET


@pytest.mark.usefixtures("api_only")
def test_the_handler_stands_down_once_root_is_configured(
    clean_logging: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_root_handlers(monkeypatch)
    logger = _logging.get_logger("application_sdk.a")
    assert len(clean_logging.handlers) == 1
    seen: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    monkeypatch.setattr(logging.getLogger(), "handlers", [_Collect()])
    logger.info("once")
    assert [r.getMessage() for r in seen] == ["once"]
    assert "once" not in capsys.readouterr().err


def test_the_full_sdk_never_configures_stdlib_logging(
    clean_logging: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_root_handlers(monkeypatch)
    from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter

    assert isinstance(_logging.get_logger("application_sdk.a"), AtlanLoggerAdapter)
    assert clean_logging.handlers == []
    assert _logging._configure_api_only_logging.cache_info().currsize == 0
