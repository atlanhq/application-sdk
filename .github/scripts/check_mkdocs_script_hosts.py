"""Require every `extra_javascript` entry in mkdocs.yml to be a local file.

A remote script runs on every docs page and hands its host the site:
polyfill.io was listed here after its domain changed hands in 2024 and began
serving malicious JavaScript (FND-4243). Vendor scripts under
`docs/static/javascripts/` instead of loading them from a CDN.

An entry passes only if it is a plain relative path: letters, digits and
`._-/`, starting with a letter, digit or `_`, with no `//`. That rules out
schemes, protocol-relative URLs, and the whitespace or backslash forms browsers
also resolve to another host.

Usage: check_mkdocs_script_hosts.py [mkdocs.yml ...]
Exits 1 and prints one line per rejected entry, by index.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Sequence

import yaml

DEFAULT_CONFIG = Path("mkdocs.yml")

_LOCAL_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*")


class _MkdocsLoader(yaml.SafeLoader):
    """SafeLoader that loads mkdocs' `!!python/...` and `!ENV` tags as None.

    mkdocs configs use those tags (e.g. superfences' `format`); nothing is
    imported or executed.
    """


def _inert(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> None:
    return None


_MkdocsLoader.add_multi_constructor("tag:yaml.org,2002:python/", _inert)
_MkdocsLoader.add_multi_constructor("!", _inert)


def _is_local(entry: object) -> bool:
    path = entry.get("path") if isinstance(entry, dict) else entry
    return (
        isinstance(path, str) and bool(_LOCAL_PATH.fullmatch(path)) and "//" not in path
    )


def find_violations(config_text: str) -> list[str]:
    """Return a message per `extra_javascript` entry that is not a local file."""
    config = yaml.load(config_text, Loader=_MkdocsLoader)  # noqa: S506 - SafeLoader subclass
    if not isinstance(config, dict):
        return ["mkdocs config must be a YAML mapping"]
    entries = config.get("extra_javascript") or []
    if not isinstance(entries, list):
        return ["extra_javascript must be a list"]
    return [
        f"extra_javascript[{index}] is not a local path; vendor the script "
        "under docs/static/javascripts/"
        for index, entry in enumerate(entries)
        if not _is_local(entry)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    paths = [Path(a) for a in args] or [DEFAULT_CONFIG]
    failed = False
    for path in paths:
        for message in find_violations(path.read_text(encoding="utf-8")):
            print(f"{path}: {message}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
