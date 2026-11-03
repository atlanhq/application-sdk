"""The mkdocs script-host check rejects third-party `extra_javascript` (FND-4243)."""

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
POLYFILL = "https://polyfill.io/v3/polyfill.min.js?features=es6"
MATHJAX = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"

PRE_FIX_CONFIG = f"""\
markdown_extensions:
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
extra_javascript:
  - static/javascripts/mathjax.js
  - {POLYFILL}
  - {MATHJAX}
"""


def _rejected(index: int, host: str) -> str:
    return (
        f"extra_javascript[{index}] (host {host}) is neither a plain local "
        "path nor a credential-free https URL on a host in ALLOWED_HOSTS; "
        "vendor it under docs/static/javascripts/"
    )


def test_repo_mkdocs_config_passes() -> None:
    config = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert check.find_violations(config) == []


def test_pre_fix_config_is_rejected() -> None:
    assert check.find_violations(PRE_FIX_CONFIG) == [
        _rejected(1, "polyfill.io"),
        _rejected(2, "cdn.jsdelivr.net"),
    ]


@pytest.mark.parametrize(
    "entry",
    [
        "http://example.com/a.js",
        "//example.com/a.js",
        "data:text/javascript,alert(1)",
        "javascript:alert(1)",
        "{path: 'https://example.com/a.js', type: module}",
        # Browsers strip leading whitespace and read `\\` as `/`, so each of
        # these loads from evil.example while urlsplit sees no scheme.
        '"\\t//evil.example/x.js"',
        '" //evil.example/x.js"',
        '"/\\\\evil.example/x.js"',
        '"\\\\\\\\evil.example/x.js"',
        # Outside the plain-relative allowlist: site-absolute and parent paths.
        "/js/a.js",
        "../a.js",
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
    for userinfo in ("user:pw@", "user@", ":pw@"):
        with_creds = f"extra_javascript:\n  - https://{userinfo}cdn.example.com/a.js\n"
        assert len(check.find_violations(with_creds)) == 1


@pytest.mark.parametrize(
    "entry",
    [
        "https://example.com/a.js?token=SENTINEL",
        "https://user:SENTINEL@example.com/a.js",
        "https://example.com/SENTINEL.js#SENTINEL",
        "'\\tSENTINEL'",
        "https://[SENTINEL/a.js",
        "{SENTINEL: x}",
    ],
)
def test_messages_never_echo_the_entry(entry: str) -> None:
    violations = check.find_violations(f"extra_javascript:\n  - {entry}\n")
    assert len(violations) == 1
    assert "SENTINEL".lower() not in violations[0].lower()


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
        f"{bad}: {_rejected(1, 'polyfill.io')}",
        f"{bad}: {_rejected(2, 'cdn.jsdelivr.net')}",
    ]

    good = tmp_path / "ok.yml"
    good.write_text("extra_javascript:\n  - a.js\n", encoding="utf-8")
    assert check.main([str(good)]) == 0
