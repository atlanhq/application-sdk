"""Tests for gen_app_api.py — an app's generated api/ package."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gen_app_api as gen  # noqa: E402

_PYPROJECT = """
[project]
name = "atlan-demo-app"
version = "1.4.0"
dependencies = ["atlan-application-sdk[sql]>=3.40.0,<4.0.0"]

[tool.atlan-app-api]
handler = "app.handler:DemoHandler"
data = ["app/sql/*.sql"]
dependencies = ["pymysql>=1.1"]
extras = ["sql"]
"""


def _app(tmp_path: Path, handler: str, pyproject: str = _PYPROJECT) -> Path:
    (tmp_path / "app/sql").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(pyproject)
    (tmp_path / "app/handler.py").write_text(handler)
    (tmp_path / "app/client.py").write_text("from .failures import Boom\n")
    (tmp_path / "app/failures.py").write_text("class Boom(Exception): ...\n")
    (tmp_path / "app/worker.py").write_text("import temporalio\n")
    (tmp_path / "app/sql/check.sql").write_text("SELECT 1\n")
    return tmp_path


def test_the_closure_follows_relative_imports_only_inside_app(tmp_path: Path) -> None:
    root = _app(
        tmp_path, "from .client import C\nfrom application_sdk.handler import Handler\n"
    )
    files, absolute = gen.closure(root, gen.load(root))
    assert files == [
        "app/client.py",
        "app/failures.py",
        "app/handler.py",
        "app/sql/check.sql",
    ]
    assert absolute == []


def test_an_absolute_app_import_is_reported_and_fixed(tmp_path: Path) -> None:
    root = _app(tmp_path, "from app.client import C\n")
    assert gen.main(["check", "--root", str(root)]) == 1
    assert gen.main(["fix", "--root", str(root)]) == 0
    assert (root / "app/handler.py").read_text() == "from .client import C\n"
    assert gen.main(["check", "--root", str(root)]) == 0


def test_stage_ships_the_app_files_as_the_package(tmp_path: Path) -> None:
    root = _app(tmp_path / "repo", "from .client import C\n")
    out = tmp_path / "staged"
    gen.stage(root, gen.load(root), out)
    package = out / "atlan_demo_api"
    assert sorted(
        p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file()
    ) == [
        "__init__.py",
        "client.py",
        "failures.py",
        "handler.py",
        "sql/check.sql",
    ]
    assert (package / "handler.py").read_text() == (root / "app/handler.py").read_text()
    init = (package / "__init__.py").read_text()
    assert (
        "from .handler import DemoHandler" in init and "handler = DemoHandler()" in init
    )
    pyproject = (out / "pyproject.toml").read_text()
    assert 'name = "atlan-demo-api"' in pyproject
    assert 'version = "1.4.0"' in pyproject
    assert '"atlan-application-sdk-api[sql]>=3.40.0,<4.0.0",' in pyproject
    assert '"pymysql>=1.1",' in pyproject
    assert 'demo = "atlan_demo_api:handler"' in pyproject


def test_stage_refuses_absolute_app_imports(tmp_path: Path) -> None:
    root = _app(tmp_path / "repo", "from app.client import C\n")
    try:
        gen.stage(root, gen.load(root), tmp_path / "staged")
    except ValueError as exc:
        assert "absolute app imports" in str(exc)
    else:
        raise AssertionError("staged a handler that imports app absolutely")


def test_a_git_pinned_sdk_becomes_a_direct_reference(tmp_path: Path) -> None:
    pinned = _PYPROJECT + (
        "\n[tool.uv.sources]\natlan-application-sdk-api = "
        '{ git = "https://github.com/atlanhq/application-sdk.git", rev = "abc123", '
        'subdirectory = "packages/api" }\n'
    )
    root = _app(tmp_path, "from .client import C\n", pinned)
    pyproject = gen._pyproject(gen.load(root))  # noqa: SLF001
    assert (
        '"atlan-application-sdk-api[sql] @ git+https://github.com/atlanhq/'
        'application-sdk.git@abc123#subdirectory=packages/api",'
    ) in pyproject
    assert "allow-direct-references = true" in pyproject


def test_a_repo_without_the_config_is_not_using_this_layout(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x-app"\nversion = "1"\n'
    )
    assert gen.main(["check", "--root", str(tmp_path)]) == 0


def test_nested_modules_get_the_right_number_of_dots(tmp_path: Path) -> None:
    root = _app(tmp_path, "from .sub.helper import H\n")
    (root / "app/sub").mkdir()
    (root / "app/sub/__init__.py").write_text("")
    (root / "app/sub/helper.py").write_text("from app.failures import Boom\n")
    gen.main(["fix", "--root", str(root)])
    assert (root / "app/sub/helper.py").read_text() == "from ..failures import Boom\n"


def test_fix_moves_run_in_thread_off_the_temporal_path(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        "from application_sdk.execution.heartbeat import run_in_thread\n"
        "from .client import C\n",
    )
    gen.main(["fix", "--root", str(root)])
    assert (
        (root / "app/handler.py")
        .read_text()
        .startswith("from application_sdk.common.concurrency import run_in_thread\n")
    )


def test_fix_removes_logging_from_handler_code(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        "from application_sdk.observability.logger_adaptor import get_logger\n"
        "from .client import C\n"
        "\n"
        "logger = get_logger(__name__)\n"
        "\n"
        "\n"
        "def f(x):\n"
        "    try:\n"
        "        return x()\n"
        "    except ValueError as e:\n"
        "        logger.error(\n"
        "            'failed: %s',\n"
        "            e,\n"
        "        )\n"
        "    logger.info('done')\n"
        "    return None\n",
    )
    assert gen.main(["fix", "--root", str(root)]) == 0
    assert (root / "app/handler.py").read_text() == (
        "from .client import C\n"
        "\n"
        "\n"
        "\n"
        "def f(x):\n"
        "    try:\n"
        "        return x()\n"
        "    except ValueError as e:\n"
        "        pass\n"
        "    return None\n"
    )


def test_fix_refuses_to_leave_a_dangling_logger(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        "import logging\nlogger = logging.getLogger(__name__)\nhelper(logger)\nfrom .client import C\n",
    )
    try:
        gen.main(["fix", "--root", str(root)])
    except ValueError as exc:
        assert "still used" in str(exc)
    else:
        raise AssertionError("fix left a reference to a removed logger")
