"""The conformance package declares its version in one place.

``pyproject.toml`` is the only file the release train rewrites
(``.github/scripts/release_train.py``); ``conformance.__version__`` is read back
from the installed distribution. Before FND-4672 the version also lived as a
literal in ``conformance/__init__.py``, and nothing checked that the two agreed.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

import conformance

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _declared() -> str:
    text = (_PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)["project"]["version"]


def test_version_agrees_with_pyproject_and_installed_metadata() -> None:
    assert (
        conformance.__version__
        == version("atlan-application-sdk-conformance")
        == _declared()
    )


def test_init_does_not_restate_the_version() -> None:
    init = (_PACKAGE_ROOT / "conformance" / "__init__.py").read_text(encoding="utf-8")
    assert f'"{_declared()}"' not in init
