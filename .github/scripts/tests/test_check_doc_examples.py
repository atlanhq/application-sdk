"""The docs' ```python blocks must parse and import only real SDK names (FND-4272)."""

from __future__ import annotations

from pathlib import Path

import check_doc_examples as check
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def src(tmp_path: Path) -> Path:
    """A miniature `application_sdk` source tree."""
    pkg = tmp_path / "application_sdk"
    (pkg / "lazy").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "plain.py").write_text(
        "from os import path as joined\n"
        "CONSTANT = 1\n"
        "Alias: type = int\n"
        "class Thing: ...\n"
        "def helper(): ...\n"
        "if True:\n"
        "    class Guarded: ...\n"
        "try:\n"
        "    from json import loads\n"
        "except ImportError:\n"
        "    pass\n"
        "_TABLE = {'NotExported': 1}\n"
    )
    (pkg / "lazy" / "__init__.py").write_text(
        "_LAZY: dict[str, tuple[str, str]] = {'Eager': ('m', 'Eager')}\n"
        "__all__ = ['Listed']\n"
        "def __getattr__(name):\n"
        "    if name == 'Compared':\n"
        "        return 1\n"
        "    raise AttributeError(name)\n"
    )
    (pkg / "lazy" / "sub.py").write_text("")
    return pkg


def _doc(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "page.md"
    path.write_text(text)
    return path


def _problems(tmp_path: Path, src: Path, text: str) -> list[str]:
    path = _doc(tmp_path, text)
    return [line.removeprefix(f"{path}:") for line in check.check_file(path, src)]


def test_repo_docs_pass() -> None:
    failures = [
        failure
        for path in sorted((REPO_ROOT / "docs").rglob("*.md"))
        for failure in check.check_file(path, REPO_ROOT / "application_sdk")
    ]
    assert failures == []


def test_unparseable_block_reports_the_file_line(tmp_path: Path, src: Path) -> None:
    text = "# Title\n\n```python\nx = 1\nexcept ValueError:\n    pass\n```\n"
    [problem] = _problems(tmp_path, src, text)
    assert problem.startswith("5: does not parse")


@pytest.mark.parametrize(
    "statement",
    [
        "from application_sdk.plain import Thing, CONSTANT, helper, Guarded, loads, joined, Alias",
        "import application_sdk.plain",
        "import application_sdk.lazy.sub as sub",
        "from application_sdk.lazy import Eager, Listed, Compared, sub",
        "from application_sdk import plain, lazy",
        "from application_sdk.plain import *",
        "from os.path import join",
        "from .relative import anything",
    ],
)
def test_resolvable_imports_pass(tmp_path: Path, src: Path, statement: str) -> None:
    assert _problems(tmp_path, src, f"```python\n{statement}\n```\n") == []


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("import application_sdk.gone", "no module application_sdk.gone"),
        ("from application_sdk.gone import X", "no module application_sdk.gone"),
        (
            "from application_sdk.plain import Missing",
            "application_sdk.plain has no name Missing",
        ),
        # Dict keys count only for a module with a lazy __getattr__.
        (
            "from application_sdk.plain import NotExported",
            "application_sdk.plain has no name NotExported",
        ),
        (
            "from application_sdk.lazy import Missing",
            "application_sdk.lazy has no name Missing",
        ),
        # A submodule of a plain module is not an attribute of it.
        (
            "from application_sdk.plain import sub",
            "application_sdk.plain has no name sub",
        ),
    ],
)
def test_unresolvable_imports_fail(
    tmp_path: Path, src: Path, statement: str, message: str
) -> None:
    assert _problems(tmp_path, src, f"\n```python\nx = 1\n{statement}\n```\n") == [
        f"4: {message}"
    ]


def test_v2_block_marker_skips_imports_but_not_parsing(
    tmp_path: Path, src: Path
) -> None:
    marked = f"{check.V2_MARKER}\n```python\nfrom application_sdk.gone import X\n```\n"
    assert _problems(tmp_path, src, marked) == []
    broken = (
        f"{check.V2_MARKER}\n```python\nfrom application_sdk.gone import ...\n```\n"
    )
    assert [p.split(":")[1] for p in _problems(tmp_path, src, broken)] == [
        " does not parse"
    ]


def test_marker_must_be_directly_above_the_fence(tmp_path: Path, src: Path) -> None:
    text = f"{check.V2_MARKER}\n\n```python\nfrom application_sdk.gone import X\n```\n"
    assert _problems(tmp_path, src, text) == ["4: no module application_sdk.gone"]


def test_v2_comment_skips_only_that_import(tmp_path: Path, src: Path) -> None:
    text = (
        "```python\n"
        "from application_sdk.gone import (  # v2\n"
        "    X,\n"
        ")\n"
        "from application_sdk.also_gone import Y\n"
        "```\n"
    )
    assert _problems(tmp_path, src, text) == ["5: no module application_sdk.also_gone"]


def test_indented_fence_is_dedented(tmp_path: Path, src: Path) -> None:
    text = "!!! note\n    ```python\n    if True:\n        from application_sdk.gone import X\n    ```\n"
    assert _problems(tmp_path, src, text) == ["4: no module application_sdk.gone"]


def test_only_python_blocks_are_checked(tmp_path: Path, src: Path) -> None:
    text = (
        "```bash\nnot python (\n```\n"
        "```\nnot python (\n```\n"
        "````markdown\n```python\nnot python (\n```\n````\n"
        "~~~py\nfrom application_sdk.gone import X\n~~~\n"
    )
    assert _problems(tmp_path, src, text) == ["13: no module application_sdk.gone"]


def test_main_exit_codes(
    tmp_path: Path, src: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ok.md").write_text(
        "```python\nfrom application_sdk.plain import Thing\n```\n"
    )
    assert check.main(["--docs", str(docs), "--src", str(src)]) == 0

    (docs / "bad.md").write_text(
        "```python\nfrom application_sdk.plain import Nope\n```\n"
    )
    assert check.main(["--docs", str(docs), "--src", str(src)]) == 1
    out = capsys.readouterr()
    assert out.out == f"{docs / 'bad.md'}:2: application_sdk.plain has no name Nope\n"
    assert check.V2_MARKER in out.err
