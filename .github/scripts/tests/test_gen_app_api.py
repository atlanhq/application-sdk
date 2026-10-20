"""Tests for gen_app_api.py — an app's generated api/ package."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_api_surface as surface  # noqa: E402 — the SDK's walker, for parity
import gen_app_api as gen  # noqa: E402


@pytest.fixture(autouse=True)
def _ruff_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fix`` requires ruff; CI has none on PATH, so stand in a clean one.

    Tests of what ruff reports override ``gen.ruff_findings`` themselves.
    """
    monkeypatch.setattr(gen, "_ruff", lambda: "ruff")
    monkeypatch.setattr(gen, "ruff_findings", lambda root, files: set())


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
    assert (
        '[project.entry-points."atlan.app_server"]\ndemo = "atlan_demo_api:get_asgi_app"'
        in pyproject
    )
    assert "def get_asgi_app()" in init and 'app_name="demo"' in init


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


def test_a_repo_with_no_pyproject_has_nothing_to_check(tmp_path: Path) -> None:
    """Every app's CI may run check; a repo with no root pyproject must pass it."""
    assert gen.load(tmp_path) is None
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
        "        raise C() from e\n"
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
        "        raise C() from e\n"
        "    return None\n"
    )


@pytest.mark.parametrize(
    ("handler", "problem"),
    [
        (
            "import logging\nlogger = logging.getLogger(__name__)\n"
            "def f():\n    y = 1; logger.info('x')\n    return y\n",
            "shares its line with other statements",
        ),
        (
            "from application_sdk.observability.metrics_adaptor import get_logger, get_metrics\n"
            "M = get_metrics()\n",
            "imports ['get_logger'] together with ['get_metrics']",
        ),
        (
            "import logging\nclass H:\n    def __init__(self):\n"
            "        self.logger = logging.getLogger(__name__)\n",
            "a logger stored on an attribute",
        ),
        (
            "import logging\nlogger = logging.getLogger(__name__)\n"
            "def f():\n    try:\n        return 1\n    except ValueError:\n"
            "        logger.error('x')\n",
            "leaves `except: pass`",
        ),
    ],
)
def test_fix_refuses_unsafe_deletions_and_changes_nothing(
    tmp_path: Path, handler: str, problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _app(tmp_path, handler)
    assert gen.main(["fix", "--root", str(root)]) == 1
    assert problem in capsys.readouterr().out
    assert (root / "app/handler.py").read_text() == handler


def test_fix_keeps_an_annotated_logger_out_and_reports_each_deletion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _app(
        tmp_path,
        "import logging\nlogger: logging.Logger = logging.getLogger(__name__)\n"
        "def f():\n    logger.info('x')\n    return 1\n",
    )
    assert gen.main(["fix", "--root", str(root)]) == 0
    assert "logger" not in (root / "app/handler.py").read_text()
    out = capsys.readouterr().out
    assert "removed app/handler.py:4: logger.info('x')" in out


def test_check_reports_logging_in_the_handler_module(tmp_path: Path) -> None:
    root = _app(tmp_path, "import logging\nfrom .client import C\nlogging.info('x')\n")
    problems = gen.check(root, gen.load(root))
    assert any(
        "app/handler.py:3: logs in the hosted handler module (P060)" in p
        for p in problems
    )


def test_fix_refuses_to_leave_a_dangling_logger(tmp_path: Path) -> None:
    handler = (
        "import logging\nlogger = logging.getLogger(__name__)\nhelper(logger)\n"
        "from .client import C\n"
    )
    root = _app(tmp_path, handler)
    assert gen.main(["fix", "--root", str(root)]) == 1
    assert (root / "app/handler.py").read_text() == handler


def test_fix_keeps_logging_in_shared_files_and_moves_the_logger_import(
    tmp_path: Path,
) -> None:
    root = _app(tmp_path, "from .client import C\n")
    shared = (
        "from application_sdk.observability.logger_adaptor import get_logger\n"
        "from .failures import Boom\n"
        "\n"
        "logger = get_logger(__name__)\n"
        "\n"
        "\n"
        "def connect():\n"
        "    logger.info('connecting')\n"
    )
    (root / "app/client.py").write_text(shared)
    assert gen.main(["check", "--root", str(root)]) == 1
    assert gen.main(["fix", "--root", str(root)]) == 0
    assert (root / "app/client.py").read_text() == shared.replace(
        "application_sdk.observability.logger_adaptor", "application_sdk.common.logger"
    )
    assert gen.main(["check", "--root", str(root)]) == 0


def test_check_refuses_the_process_wide_app_name(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        "from application_sdk.constants import APPLICATION_NAME\n"
        "from .client import C\n",
    )
    assert gen.check(root, gen.load(root)) == [
        "app/handler.py:1: APPLICATION_NAME is the host's own name on the "
        "consolidated host; use `self.context.app_name`"
    ]


def test_the_name_override_sets_the_routing_name(tmp_path: Path) -> None:
    pyproject = _PYPROJECT.replace(
        'handler = "app.handler:DemoHandler"',
        'handler = "app.handler:DemoHandler"\nname = "ai-demo"',
    )
    root = _app(tmp_path, "from .client import C\n", pyproject)
    config = gen.load(root)
    assert config.name == "ai-demo"
    assert config.package == "atlan_demo_api"


def test_stage_command_writes_the_package(tmp_path: Path) -> None:
    root = _app(tmp_path / "repo", "from .client import C\n")
    out = tmp_path / "staged"
    assert gen.main(["stage", "--root", str(root), "--out", str(out)]) == 0
    assert (out / "pyproject.toml").is_file()
    assert (out / "atlan_demo_api" / "handler.py").is_file()


def test_check_refuses_the_app_name_env_var(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        'import os\nQUEUE = os.environ["ATLAN_APPLICATION_NAME"]\nfrom .client import C\n',
    )
    problems = gen.check(root, gen.load(root))
    assert len(problems) == 1 and problems[0].startswith("app/handler.py:2:")


def test_stage_ships_the_generated_contract_files_only(tmp_path: Path) -> None:
    root = _app(tmp_path / "repo", "from .client import C\n")
    gen_dir = root / "app/generated"
    gen_dir.mkdir()
    (gen_dir / "__init__.py").write_text("from .scaffold import X\n")
    (gen_dir / "manifest.json").write_text("{}")
    (gen_dir / "demo.json").write_text("{}")
    (gen_dir / "_input.py").write_text("AppInputContract = None\n")
    (gen_dir / "_e2e_base.py").write_text("import application_sdk.testing\n")
    (gen_dir / "crawler").mkdir()
    (gen_dir / "crawler/manifest.json").write_text("{}")
    out = tmp_path / "staged"
    gen.stage(root, gen.load(root), out)
    shipped = sorted(
        p.relative_to(out / "atlan_demo_api/generated").as_posix()
        for p in (out / "atlan_demo_api/generated").rglob("*")
        if p.is_file()
    )
    assert shipped == [
        "__init__.py",
        "_input.py",
        "crawler/manifest.json",
        "demo.json",
        "manifest.json",
    ]
    # Not the app's own __init__.py, which may import e2e tooling.
    assert (out / "atlan_demo_api/generated/__init__.py").read_text() == ""


def test_check_refuses_a_compute_manifest_hook(tmp_path: Path) -> None:
    root = _app(tmp_path, "from .client import C\n")
    (root / "app/crawler").mkdir()
    (root / "app/crawler/core.py").write_text(
        "async def compute_manifest(manifest, fe_inputs):\n    return manifest\n"
    )
    problems = gen.check(root, gen.load(root))
    assert problems == [
        "app/crawler/core.py: compute_manifest runs app code per request; the "
        "consolidated host serves static manifests only, so this app cannot be "
        "hosted yet"
    ]


def test_a_relative_import_in_a_package_init_ships_its_sibling(tmp_path: Path) -> None:
    root = _app(tmp_path, "from .errors import Boom2\nfrom .client import C\n")
    (root / "app/errors").mkdir()
    (root / "app/errors/__init__.py").write_text("from .mapping import Boom2\n")
    (root / "app/errors/mapping.py").write_text("class Boom2(Exception): ...\n")
    files, absolute = gen.closure(root, gen.load(root))
    assert "app/errors/mapping.py" in files
    assert absolute == []


def test_the_content_hash_moves_only_with_what_the_host_installs(
    tmp_path: Path,
) -> None:
    root = _app(tmp_path, "from .client import C\n")
    config = gen.load(root)
    before = gen.content_hash(root, config)
    assert before == gen.content_hash(root, config)  # deterministic
    (root / "app/worker.py").write_text("import temporalio  # a worker-only change\n")
    assert gen.content_hash(root, config) == before
    (root / "app/client.py").write_text("from .failures import Boom\nX = 1\n")
    assert gen.content_hash(root, config) != before


def test_the_content_hash_ignores_the_app_version_but_not_its_dependencies(
    tmp_path: Path,
) -> None:
    root = _app(tmp_path, "from .client import C\n")
    before = gen.content_hash(root, gen.load(root))
    pyproject = root / "pyproject.toml"
    pyproject.write_text(_PYPROJECT.replace('version = "1.4.0"', 'version = "1.5.0"'))
    assert gen.load(root).version == "1.5.0"
    assert gen.content_hash(root, gen.load(root)) == before  # a release, nothing served
    pyproject.write_text(_PYPROJECT.replace('"pymysql>=1.1"', '"pymysql>=1.2"'))
    assert gen.content_hash(root, gen.load(root)) != before  # the host installs more


def test_the_api_floor_is_the_sdk_version_the_app_locks(tmp_path: Path) -> None:
    root = _app(tmp_path, "from .client import C\n")
    (root / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "atlan-application-sdk"\nversion = "3.46.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    pyproject = gen._pyproject(gen.load(root))  # noqa: SLF001
    assert '"atlan-application-sdk-api[sql]>=3.46.0,<4.0.0",' in pyproject


def test_without_a_lock_the_declared_range_is_kept(tmp_path: Path) -> None:
    root = _app(tmp_path, "from .client import C\n")
    assert '"atlan-application-sdk-api[sql]>=3.40.0,<4.0.0",' in gen._pyproject(
        gen.load(root)
    )  # noqa: SLF001


def test_fix_leaves_import_text_inside_strings_alone(tmp_path: Path) -> None:
    handler = (
        '"""Usage::\n\n    from app.client import C\n"""\n' "from app.client import C\n"
    )
    root = _app(tmp_path, handler)
    assert gen.main(["fix", "--root", str(root)]) == 0
    assert (root / "app/handler.py").read_text() == (
        '"""Usage::\n\n    from app.client import C\n"""\nfrom .client import C\n'
    )


def test_fix_without_ruff_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handler = "import logging\nfrom .client import C\nlogging.info('x')\n"
    root = _app(tmp_path, handler)
    monkeypatch.setattr(gen, "_ruff", lambda: None)
    assert gen.main(["fix", "--root", str(root)]) == 1
    assert "needs ruff" in capsys.readouterr().out
    assert (root / "app/handler.py").read_text() == handler


def test_fix_restores_every_file_when_ruff_finds_what_it_introduced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handler = "import logging\nfrom app.client import C\nlogging.info('x')\n"
    root = _app(tmp_path, handler)
    calls = iter([set(), {("app/handler.py", "F401 `C` imported but unused")}])
    monkeypatch.setattr(gen, "ruff_findings", lambda root, files: next(calls))
    assert gen.main(["fix", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "fix would leave app/handler.py: F401" in out
    assert "removed" not in out
    assert (root / "app/handler.py").read_text() == handler


def test_fix_removes_a_logging_import_of_only_loggers(tmp_path: Path) -> None:
    root = _app(
        tmp_path,
        "from logging import getLogger\nfrom .client import C\n"
        "logger = getLogger(__name__)\n",
    )
    assert gen.main(["fix", "--root", str(root)]) == 0
    assert (root / "app/handler.py").read_text() == "from .client import C\n"


def test_fix_refuses_a_logging_import_that_brings_other_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    handler = (
        "from logging import Formatter, getLogger\nfrom .client import C\n"
        "logger = getLogger(__name__)\nFMT = Formatter('%(message)s')\n"
    )
    root = _app(tmp_path, handler)
    assert gen.main(["fix", "--root", str(root)]) == 1
    assert "together with ['Formatter']" in capsys.readouterr().out
    assert (root / "app/handler.py").read_text() == handler


def test_a_logging_import_of_no_loggers_is_left_alone(tmp_path: Path) -> None:
    handler = (
        "from logging import Formatter\nfrom .client import C\nFMT = Formatter()\n"
    )
    root = _app(tmp_path, handler)
    assert gen.main(["fix", "--root", str(root)]) == 0
    assert (root / "app/handler.py").read_text() == handler


# The app walker reimplements check_api_surface's (gen_app_api.py runs alone in
# app repos and cannot import it). Hold the two to the same answers.
_PARITY_TREE = {
    "app/__init__.py": "",
    "app/handler.py": (
        "from .client import C\n"
        "from . import sub\n"
        "from .sub import thing, NAME\n"
        "from .sub.deep import *\n"
        "import app.models\n"
        "from app import failures\n"
        "from application_sdk.handler import Handler\n"
        "def lazy():\n"
        "    try:\n"
        "        from .worker import W\n"
        "    except ImportError:\n"
        "        W = None\n"
    ),
    # A module and a package of one name: the package is what imports.
    "app/client.py": "SHADOWED = True\n",
    "app/client/__init__.py": "from .transport import T\n",
    "app/client/transport.py": "from ..failures import Boom\n",
    "app/failures.py": "class Boom(Exception): ...\n",
    "app/models.py": "",
    "app/worker.py": "",
    "app/sub/__init__.py": "NAME = 1\nfrom . import thing\n",
    "app/sub/thing.py": "from ..models import *\n",
    "app/sub/deep.py": "from .. import client\nimport app.sub.thing as t\n",
    # The one known divergence (asserted below): an import only a type checker
    # runs. check_api_surface skips it; gen_app_api ships its target anyway.
    "app/typed.py": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .worker import W\n"
        "else:\n"
        "    from .models import *\n"
    ),
}

#: Files the app walker reaches that check_api_surface does not, and why.
_KNOWN_DIVERGENCE = {
    # ``if TYPE_CHECKING:`` bodies: check_api_surface skips them (never run on
    # the host); gen_app_api's ``ast.walk`` follows them, so the handler wheel
    # ships the target too. Conservative (an extra file, never a missing one).
    "app/typed.py": {"app/worker.py"},
}


def _parity_root(tmp_path: Path) -> Path:
    for rel, text in _PARITY_TREE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


def test_module_resolution_matches_check_api_surface(tmp_path: Path) -> None:
    root = _parity_root(tmp_path)
    assert gen._module_file(root, "app.client") == "app/client/__init__.py"
    names = {
        "app",
        "app.nope",
        *(surface.module_of(rel) for rel in _PARITY_TREE),
        *(f"{surface.module_of(rel)}.x" for rel in _PARITY_TREE),
    }
    for name in sorted(names):
        assert gen._module_file(root, name) == surface.file_of(root, name), name

    for rel in sorted(_PARITY_TREE):
        if surface.file_of(root, surface.module_of(rel)) != rel:
            continue  # the shadowed app/client.py never imports
        module = surface.module_of(rel)
        is_package = rel.endswith("__init__.py")
        tree = ast.parse((root / rel).read_text())
        ours = {
            gen._module_file(root, target)
            for _node, target, _relative in gen._app_imports(tree, module, is_package)
        } - {None}
        theirs = {
            surface.file_of(root, name)
            for _node, name, _top, _guarded, _required in surface._imports(
                tree, module, is_package
            )
            if name.split(".")[0] == gen.APP
        } - {None}
        assert ours - theirs == _KNOWN_DIVERGENCE.get(rel, set()), rel
        assert theirs <= ours, rel


def test_a_dependency_marker_with_quotes_is_valid_toml(tmp_path: Path) -> None:
    """A PEP 508 marker carries double quotes; the generated pyproject must parse."""
    marker = 'tomli>=2; python_version < "3.12"'
    root = _app(
        tmp_path,
        "from .client import C\n",
        _PYPROJECT.replace(
            'dependencies = ["pymysql>=1.1"]', f"dependencies = [{marker!r}]"
        ),
    )
    parsed = tomllib.loads(gen._pyproject(gen.load(root)))  # noqa: SLF001
    assert marker in parsed["project"]["dependencies"]


@pytest.mark.parametrize(
    ("declared", "locked", "expected"),
    [
        # An exact pin is the app's statement of what it runs: kept as is.
        ("==3.40.0", "3.40.0", "==3.40.0"),
        # A compatible release keeps its upper bound under the raised floor.
        ("~=3.40", "3.46.0", ">=3.46.0,~=3.40"),
        # The ordinary range: floor raised, upper bound kept.
        (">=3.40.0,<4.0.0", "3.46.0", ">=3.46.0,<4.0.0"),
    ],
)
def test_the_floor_keeps_the_apps_other_constraints(
    declared: str, locked: str, expected: str
) -> None:
    assert gen._floored(declared, locked) == expected  # noqa: SLF001


def test_a_plain_import_of_app_is_reported_as_a_manual_fix(tmp_path: Path) -> None:
    """``fix`` rewrites ``from app... import`` only: ``import app.client`` binds
    ``app`` and its uses would need rewriting too, so check says so instead of
    sending the author to a ``fix`` that leaves it untouched."""
    root = _app(tmp_path, "import app.client\n")
    problems = gen.check(root, gen.load(root))
    absolute = [p for p in problems if "absolute import of app.client" in p]
    assert len(absolute) == 1, problems
    assert "by hand" in absolute[0]
    assert "run `fix`" not in absolute[0]


@pytest.mark.parametrize(
    ("source", "fixed"),
    [
        ("import app.client as cl\n", "from . import client as cl\n"),
        ("import app.sub.thing as t\n", "from .sub import thing as t\n"),
        (
            "import app.client as cl, app.sub.thing as t  # both\n",
            "from . import client as cl\nfrom .sub import thing as t  # both\n",
        ),
    ],
)
def test_fix_rewrites_an_aliased_import_of_app(
    tmp_path: Path, source: str, fixed: str
) -> None:
    """``import app.x.y as t`` binds only ``t``, so ``from .x import y as t`` is
    the same binding: ``fix`` rewrites it and ``check`` then passes."""
    root = _app(tmp_path, source)
    (root / "app/sub").mkdir()
    (root / "app/sub/__init__.py").write_text("")
    (root / "app/sub/thing.py").write_text("")
    assert gen.main(["fix", "--root", str(root)]) == 0
    assert (root / "app/handler.py").read_text() == fixed
    assert gen.main(["check", "--root", str(root)]) == 0


def test_fix_leaves_a_plain_import_of_app_for_a_manual_rewrite(tmp_path: Path) -> None:
    root = _app(tmp_path, "import app.client\n")
    gen.main(["fix", "--root", str(root)])
    assert (root / "app/handler.py").read_text() == "import app.client\n"
