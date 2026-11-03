"""The mkdocs script check rejects any non-local `extra_javascript` (FND-4243)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_mkdocs_script_hosts as check  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]

# The shape mkdocs.yml had before FND-4243, including the `!!python/name:` tag
# a plain safe_load refuses.
PRE_FIX_CONFIG = """\
markdown_extensions:
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
extra_javascript:
  - static/javascripts/mathjax.js
  - https://polyfill.io/v3/polyfill.min.js?features=es6
  - https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js
"""


def _rejected(index: int) -> str:
    return (
        f"extra_javascript[{index}] is not a local path; vendor the script "
        "under docs/static/javascripts/"
    )


def test_repo_mkdocs_config_passes() -> None:
    config = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert check.find_violations(config) == []


def test_pre_fix_config_is_rejected() -> None:
    assert check.find_violations(PRE_FIX_CONFIG) == [_rejected(1), _rejected(2)]


@pytest.mark.parametrize(
    "entry",
    [
        "https://example.com/a.js",
        "//example.com/a.js",
        "data:text/javascript,alert(1)",
        "{path: 'https://example.com/a.js', type: module}",
        # Browsers strip leading whitespace and read `\\` as `/`.
        '"\\t//example.com/a.js"',
        '"/\\\\example.com/a.js"',
        "/js/a.js",
        "../a.js",
        "{type: module}",
        "42",
    ],
)
def test_non_local_entries_are_rejected(entry: str) -> None:
    config = f"extra_javascript:\n  - {entry}\n"
    assert check.find_violations(config) == [_rejected(0)]


@pytest.mark.parametrize(
    "entry",
    ["static/javascripts/x.js", "js/a.mjs", "{path: js/a.mjs, type: module}"],
)
def test_local_entries_pass(entry: str) -> None:
    assert check.find_violations(f"extra_javascript:\n  - {entry}\n") == []


@pytest.mark.parametrize("config", ["", "# truncated\n", "- a\n", "just text\n"])
def test_non_mapping_config_fails_closed(config: str) -> None:
    assert check.find_violations(config) == ["mkdocs config must be a YAML mapping"]


def test_python_tags_are_not_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def record(*args: object) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(os, "system", record)
    config = "x: !!python/object/apply:os.system ['exit 1']\nextra_javascript: []\n"
    assert check.find_violations(config) == []
    assert calls == []


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "mkdocs.yml"
    bad.write_text(PRE_FIX_CONFIG, encoding="utf-8")
    assert check.main([str(bad)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{bad}: {_rejected(1)}",
        f"{bad}: {_rejected(2)}",
    ]

    good = tmp_path / "ok.yml"
    good.write_text("extra_javascript:\n  - a.js\n", encoding="utf-8")
    assert check.main([str(good)]) == 0
