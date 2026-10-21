"""Tests for check_api_surface.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_api_surface as surface  # noqa: E402

REPO = Path(__file__).resolve().parents[3]

_PYPROJECT = """
[project]
name = "atlan-application-sdk-api"
version = "0.0.0"
dependencies = ["pydantic>=2", "fastapi>=0.1"]
"""


def _tree(tmp_path: Path, files: dict[str, str], listed: list[str]) -> Path:
    (tmp_path / "packages/api").mkdir(parents=True)
    (tmp_path / "packages/api/pyproject.toml").write_text(_PYPROJECT)
    (tmp_path / "packages/api/api-files.txt").write_text(
        "# comment\n" + "\n".join(listed) + "\n"
    )
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


_BASE = {
    "application_sdk/__init__.py": "",
    "application_sdk/handler/__init__.py": "",
    "application_sdk/execution/__init__.py": "import temporalio\n",
    "application_sdk/execution/worker.py": "",
}
_LISTED = [
    "application_sdk/__init__.py",
    "application_sdk/handler/__init__.py",
    "application_sdk/handler/base.py",
]


def _messages(tmp_path: Path, base_py: str) -> list[str]:
    root = _tree(
        tmp_path, {**_BASE, "application_sdk/handler/base.py": base_py}, _LISTED
    )
    return [p.message for p in surface.check(root)]


@pytest.mark.parametrize(
    "src",
    [
        "from application_sdk.handler import x\n",
        "import pydantic\nfrom fastapi import FastAPI\nfrom starlette import status\n",
        "import json\nfrom __future__ import annotations\n",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from application_sdk.execution.worker import W\n",
        "def f():\n"
        "    try:\n"
        "        from application_sdk.execution.worker import W\n"
        "    except ModuleNotFoundError as exc:\n"
        "        if exc.name != 'application_sdk.execution':\n"
        "            raise\n",
        "def f():\n"
        "    try:\n"
        "        import temporalio\n"
        "    except (ImportError, ModuleNotFoundError):\n"
        "        pass\n",
    ],
)
def test_clean(tmp_path: Path, src: str) -> None:
    assert _messages(tmp_path, src) == []


@pytest.mark.parametrize(
    ("src", "fragment"),
    [
        ("from application_sdk.execution.worker import W\n", "not in"),
        ("from application_sdk.execution import worker\n", "not in"),
        ("from ..execution import worker\n", "not in"),
        ("import temporalio\n", "does not declare"),
        (
            "def f():\n    from application_sdk.execution.worker import W\n",
            "without a try/except ModuleNotFoundError",
        ),
        (
            "def f():\n"
            "    try:\n"
            "        from application_sdk.execution.worker import W\n"
            "    except Exception:\n"
            "        pass\n",
            "without a try/except ModuleNotFoundError",
        ),
        ("def f():\n    import temporalio\n", "does not declare"),
        ("from application_sdk.revision import X\n", "does not exist"),
        (
            "def f():\n"
            "    try:\n"
            "        from application_sdk.revision import X\n"
            "    except ModuleNotFoundError:\n"
            "        pass\n",
            "does not exist",
        ),
        ("import application_sdk.handler.nope\n", "does not exist"),
        # Only a positive TYPE_CHECKING guard skips its body: a negated one runs.
        (
            "from typing import TYPE_CHECKING\n"
            "if not TYPE_CHECKING:\n"
            "    from application_sdk.execution.worker import W\n",
            "not in",
        ),
    ],
)
def test_flagged(tmp_path: Path, src: str, fragment: str) -> None:
    messages = _messages(tmp_path, src)
    assert len(messages) == 1, messages
    assert fragment in messages[0]


def test_unlisted_parent_package(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {**_BASE, "application_sdk/handler/base.py": ""},
        ["application_sdk/__init__.py", "application_sdk/handler/base.py"],
    )
    (problem,) = surface.check(root)
    assert "parent package application_sdk/handler/__init__.py" in problem.message


def test_missing_listed_file(tmp_path: Path) -> None:
    root = _tree(tmp_path, _BASE, _LISTED)
    (problem,) = surface.check(root)
    assert "application_sdk/handler/base.py does not exist" in problem.message


def test_the_real_api_surface_is_clean() -> None:
    assert [str(p) for p in surface.check(REPO)] == []


def test_the_real_check_catches_a_worker_import(tmp_path: Path) -> None:
    """Mutation check: the real list flags a worker import added to a listed file."""
    listed = surface.listed_files(REPO)
    files = {rel: (REPO / rel).read_text() for rel in listed}
    files["application_sdk/handler/base.py"] += (
        "\nfrom application_sdk.execution import run_dev_combined\n"
    )
    root = _tree(tmp_path, files, listed)
    (tmp_path / "packages/api/pyproject.toml").write_text(
        (REPO / "packages/api/pyproject.toml").read_text()
    )
    (tmp_path / "application_sdk/execution").mkdir(parents=True, exist_ok=True)
    (tmp_path / "application_sdk/execution/__init__.py").write_text("")
    messages = [str(p) for p in surface.check(root)]
    assert any(
        "handler/base.py" in m and "application_sdk.execution" in m for m in messages
    ), messages


_GROUPED = """
[project]
name = "atlan-application-sdk-api"
version = "0.0.0"
dependencies = ["pydantic>=2"]

[project.optional-dependencies]
sql = ["sqlalchemy>=2", "httpx>=0.27"]

[tool.atlan-api.seeds]
core = ["application_sdk.handler.base"]
sql = ["application_sdk.clients.sql"]
"""


def _grouped(tmp_path: Path, handler_src: str, sql_src: str) -> list[str]:
    root = _tree(
        tmp_path,
        {
            "application_sdk/__init__.py": "",
            "application_sdk/handler/__init__.py": "",
            "application_sdk/handler/base.py": handler_src,
            "application_sdk/clients/__init__.py": "",
            "application_sdk/clients/sql.py": sql_src,
        },
        [
            "application_sdk/__init__.py",
            "application_sdk/clients/__init__.py",
            "application_sdk/clients/sql.py",
            "application_sdk/handler/__init__.py",
            "application_sdk/handler/base.py",
        ],
    )
    (root / "packages/api/pyproject.toml").write_text(_GROUPED)
    return [p.message for p in surface.check(root)]


def test_a_group_file_may_import_its_extras_deps_at_module_level(
    tmp_path: Path,
) -> None:
    assert (
        _grouped(tmp_path, "import pydantic\n", "import httpx\nimport pydantic\n") == []
    )


def test_a_core_file_may_not_import_an_extras_dep_at_module_level(
    tmp_path: Path,
) -> None:
    (message,) = _grouped(tmp_path, "import httpx\n", "")
    assert "does not declare" in message


# --- Lazy exports -----------------------------------------------------------
#
# A listed package's ``__getattr__`` over a ``name -> (module, attr)`` map
# imports its targets by string, so the import closure never sees them. The
# regression: ``credentials/__init__`` (core) lazily exported
# ``expand_dotted_keys`` from ``common.transforms``, which was not shipped, so
# ``from application_sdk.credentials import expand_dotted_keys`` failed on the
# host.

_INSTALL = '_WORKER_ONLY_PREFIXES: tuple[str, ...] = ("application_sdk.execution",)\n'

_CREDENTIALS_INIT = """
from application_sdk._install import lazy_getattr

_LAZY: dict[str, tuple[str, str]] = {
    "expand_dotted_keys": ("application_sdk.common.transforms", "expand_dotted_keys"),
}

def __getattr__(name):
    return lazy_getattr(__name__, name, _LAZY, globals())
"""

_LAZY_LISTED = [
    "application_sdk/__init__.py",
    "application_sdk/_install.py",
    "application_sdk/credentials/__init__.py",
]


def _lazy(
    tmp_path: Path, init_src: str, extra_listed: list[str] = (), **files: str
) -> list[str]:
    root = _tree(
        tmp_path,
        {
            "application_sdk/__init__.py": "",
            "application_sdk/_install.py": _INSTALL,
            "application_sdk/common/__init__.py": "",
            "application_sdk/common/transforms.py": "def expand_dotted_keys(d): ...\n",
            "application_sdk/execution/__init__.py": "",
            "application_sdk/execution/worker.py": "",
            "application_sdk/credentials/__init__.py": init_src,
            **{
                "application_sdk/" + k.replace("__", "/") + ".py": v
                for k, v in files.items()
            },
        },
        [*_LAZY_LISTED, *extra_listed],
    )
    return [p.message for p in surface.check(root)]


def test_a_lazy_export_of_an_unshipped_module_fails(tmp_path: Path) -> None:
    """The transforms regression: a core package lazily exports an unlisted module."""
    (message,) = _lazy(tmp_path, _CREDENTIALS_INIT)
    assert (
        "lazily exports expand_dotted_keys from application_sdk.common.transforms"
        in (message)
    )
    assert "not in" in message and "_WORKER_ONLY_PREFIXES" in message


def test_a_lazy_export_of_a_shipped_module_passes(tmp_path: Path) -> None:
    listed = [
        "application_sdk/common/__init__.py",
        "application_sdk/common/transforms.py",
    ]
    assert _lazy(tmp_path, _CREDENTIALS_INIT, listed) == []


def test_a_lazy_export_to_a_named_worker_only_module_passes(tmp_path: Path) -> None:
    src = _CREDENTIALS_INIT.replace(
        '("application_sdk.common.transforms", "expand_dotted_keys")',
        '("application_sdk.execution.worker", "Worker")',
    )
    assert _lazy(tmp_path, src) == []


def test_a_lazy_export_of_a_module_that_does_not_exist_fails(tmp_path: Path) -> None:
    src = _CREDENTIALS_INIT.replace("common.transforms", "common.nope")
    (message,) = _lazy(tmp_path, src)
    assert "application_sdk.common.nope, which does not exist" in message


def test_a_module_name_map_is_a_lazy_export_too(tmp_path: Path) -> None:
    """``templates/__init__`` maps a name straight to a module string."""
    src = (
        "import importlib\n"
        '_module_map = {"T": "application_sdk.common.transforms"}\n'
        "def __getattr__(name):\n"
        "    return getattr(importlib.import_module(_module_map[name]), name)\n"
    )
    (message,) = _lazy(tmp_path, src)
    assert "lazily exports T from application_sdk.common.transforms" in message


def test_lazy_getattr_submodules_are_lazy_exports(tmp_path: Path) -> None:
    """``import pkg; pkg.sub`` for the submodules passed to ``lazy_getattr``."""
    src = (
        "from application_sdk._install import lazy_getattr\n"
        "_LAZY = {}\n"
        '_EAGER_SUBMODULES = frozenset({"resolver"})\n'
        "def __getattr__(name):\n"
        "    return lazy_getattr(__name__, name, _LAZY, globals(), _EAGER_SUBMODULES)\n"
    )
    (message,) = _lazy(tmp_path, src, credentials__resolver="")
    assert (
        "lazily exports resolver from application_sdk.credentials.resolver, "
        "which is not in"
    ) in message


def test_a_dict_without_a_module_getattr_is_not_a_lazy_export(tmp_path: Path) -> None:
    src = '_MAP = {"x": ("application_sdk.common.transforms", "expand_dotted_keys")}\n'
    assert _lazy(tmp_path, src) == []


def test_a_prefix_matches_a_whole_module_not_a_name_prefix(tmp_path: Path) -> None:
    src = _CREDENTIALS_INIT.replace(
        "common.transforms", "execution_helpers"
    )  # "application_sdk.execution" must not cover "application_sdk.execution_helpers"
    (message,) = _lazy(tmp_path, src, execution_helpers="")
    assert "application_sdk.execution_helpers, which is not in" in message


def test_the_real_check_catches_the_transforms_regression(tmp_path: Path) -> None:
    """Mutation check: unlisting common/transforms.py flags the real lazy export."""
    listed = [
        rel
        for rel in surface.listed_files(REPO)
        if rel != "application_sdk/common/transforms.py"
    ]
    files = {rel: (REPO / rel).read_text() for rel in listed}
    files["application_sdk/common/transforms.py"] = (
        REPO / "application_sdk/common/transforms.py"
    ).read_text()
    root = _tree(tmp_path, files, listed)
    (root / "packages/api/pyproject.toml").write_text(
        (REPO / "packages/api/pyproject.toml").read_text()
    )
    messages = [str(p) for p in surface.check(root)]
    assert any(
        "credentials/__init__.py" in m
        and "expand_dotted_keys from application_sdk.common.transforms" in m
        for m in messages
    ), messages


def test_worker_only_prefixes_are_read_from_install() -> None:
    from application_sdk._install import (  # noqa: PLC0415 — the SDK, not a sibling script
        _WORKER_ONLY_PREFIXES,
    )

    assert surface.worker_only_prefixes(REPO) == _WORKER_ONLY_PREFIXES


# --- A core module's third-party imports ------------------------------------
#
# ``constants`` was reached only from the sql and aws seed groups, so its
# module-level ``dotenv`` import was (correctly, under that model) allowed by
# those extras. But handlers import ``constants`` directly, so it is a core seed
# now; the check then requires core to declare python-dotenv.

_DOTENV = """
[project]
name = "atlan-application-sdk-api"
version = "0.0.0"
dependencies = ["pydantic>=2"{core}]

[project.optional-dependencies]
sql = ["sqlalchemy>=2", "python-dotenv>=1.1"]

[tool.atlan-api.seeds]
core = ["application_sdk.handler.base"{core_seed}]
sql = ["application_sdk.clients.sql"]
"""


def _dotenv(tmp_path: Path, *, core_seed: bool, core_dep: bool) -> list[str]:
    root = _tree(
        tmp_path,
        {
            "application_sdk/__init__.py": "",
            "application_sdk/constants.py": "from dotenv import load_dotenv\n",
            "application_sdk/handler/__init__.py": "",
            "application_sdk/handler/base.py": "",
            "application_sdk/clients/__init__.py": "",
            "application_sdk/clients/sql.py": "import application_sdk.constants\n",
        },
        [
            "application_sdk/__init__.py",
            "application_sdk/clients/__init__.py",
            "application_sdk/clients/sql.py",
            "application_sdk/constants.py",
            "application_sdk/handler/__init__.py",
            "application_sdk/handler/base.py",
        ],
    )
    (root / "packages/api/pyproject.toml").write_text(
        _DOTENV.format(
            core=', "python-dotenv>=1.1"' if core_dep else "",
            core_seed=', "application_sdk.constants"' if core_seed else "",
        )
    )
    return [p.message for p in surface.check(root)]


def test_an_extra_reached_module_may_import_its_extras_dep(tmp_path: Path) -> None:
    assert _dotenv(tmp_path, core_seed=False, core_dep=False) == []


def test_a_core_seeded_module_needs_core_to_declare_its_dep(tmp_path: Path) -> None:
    (message,) = _dotenv(tmp_path, core_seed=True, core_dep=False)
    assert "imports dotenv" in message and "does not declare" in message


def test_a_core_seeded_module_passes_once_core_declares_it(tmp_path: Path) -> None:
    assert _dotenv(tmp_path, core_seed=True, core_dep=True) == []


def test_constants_is_core_and_core_declares_python_dotenv() -> None:
    import gen_api_files  # noqa: PLC0415

    assert "core" in gen_api_files.file_groups(REPO)["application_sdk/constants.py"]
    assert "python_dotenv" in surface.declared_requirements(REPO)
