"""The mkdocs script-host check rejects third-party `extra_javascript` (FND-4243)."""

from __future__ import annotations

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


def test_repo_mkdocs_config_passes() -> None:
    config = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert check.find_violations(config) == []


def test_pre_fix_config_is_rejected() -> None:
    violations = check.find_violations(PRE_FIX_CONFIG)
    assert len(violations) == 2
    assert "polyfill.io" in violations[0]
    assert "cdn.jsdelivr.net" in violations[1]


@pytest.mark.parametrize(
    "entry",
    [
        "http://example.com/a.js",
        "//example.com/a.js",
        "data:text/javascript,alert(1)",
        "javascript:alert(1)",
        "{path: 'https://example.com/a.js', type: module}",
    ],
)
def test_remote_entries_are_rejected(entry: str) -> None:
    assert len(check.find_violations(f"extra_javascript:\n  - {entry}\n")) == 1


@pytest.mark.parametrize(
    "entry",
    ["static/javascripts/x.js", "js/a.mjs", "{path: js/a.mjs, type: module}"],
)
def test_local_entries_pass(entry: str) -> None:
    assert check.find_violations(f"extra_javascript:\n  - {entry}\n") == []


def test_allowlisted_host_passes_only_over_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check, "ALLOWED_HOSTS", frozenset({"cdn.example.com"}))
    ok = "extra_javascript:\n  - https://cdn.example.com/a.js\n"
    plain = "extra_javascript:\n  - http://cdn.example.com/a.js\n"
    other = "extra_javascript:\n  - https://evil.example.com/a.js\n"
    assert check.find_violations(ok) == []
    assert len(check.find_violations(plain)) == 1
    assert len(check.find_violations(other)) == 1


def test_python_tags_are_not_executed() -> None:
    config = "x: !!python/object/apply:os.system ['exit 1']\nextra_javascript: []\n"
    assert check.find_violations(config) == []


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "mkdocs.yml"
    bad.write_text(PRE_FIX_CONFIG, encoding="utf-8")
    assert check.main([str(bad)]) == 1
    assert "polyfill.io" in capsys.readouterr().out

    good = tmp_path / "ok.yml"
    good.write_text("extra_javascript:\n  - a.js\n", encoding="utf-8")
    assert check.main([str(good)]) == 0
