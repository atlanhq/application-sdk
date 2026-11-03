"""Reject `extra_javascript` entries in mkdocs.yml served from unapproved hosts.

Every script listed under `extra_javascript` runs on every page of the docs
site. A third-party host there hands that host the site: polyfill.io was
listed here after its domain changed hands in 2024 and began serving
malicious JavaScript (FND-4243).

A local path (relative to `docs_dir`, letters, digits and `._-/` only, no
`//`) passes. Anything else must be an `https` URL whose host is in
`ALLOWED_HOSTS`. Local paths are matched against an allowlist rather than
"has no scheme" because browsers strip leading whitespace and treat `\\` as
`/`, so `"\t//evil.example/x.js"` or `"/\\evil.example/x.js"` load from another
host while looking schemeless. The allowlist is empty on purpose: vendor a script
into `docs/static/javascripts/` instead of loading it from a CDN. Adding a host
here is a reviewed change to this file.

Usage: check_mkdocs_script_hosts.py [mkdocs.yml ...]
Exits 1 and prints one line per offending entry when any entry is rejected.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

import yaml

ALLOWED_HOSTS: frozenset[str] = frozenset()

DEFAULT_CONFIG = Path("mkdocs.yml")

_LOCAL_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*")


class _MkdocsLoader(yaml.SafeLoader):
    """SafeLoader that tolerates mkdocs' `!!python/...` and `!ENV` tags.

    Those tags appear in mkdocs configs (e.g. superfences' `format`). They are
    loaded as inert placeholders; nothing is imported or executed.
    """


def _inert(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> None:
    return None


_MkdocsLoader.add_multi_constructor("tag:yaml.org,2002:python/", _inert)
_MkdocsLoader.add_multi_constructor("!", _inert)


def _entry_path(entry: object) -> str | None:
    """Return the script location for a string or `{path: ...}` entry."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        path = entry.get("path")
        if isinstance(path, str):
            return path
    return None


def find_violations(config_text: str) -> list[str]:
    """Return a message per `extra_javascript` entry that is not allowed."""
    config = yaml.load(config_text, Loader=_MkdocsLoader)  # noqa: S506 - SafeLoader subclass
    if not isinstance(config, dict):
        # Empty or truncated config: fail closed rather than pass vacuously.
        return ["mkdocs config must be a YAML mapping"]
    entries = config.get("extra_javascript") or []
    if not isinstance(entries, list):
        return ["extra_javascript must be a list"]

    violations: list[str] = []
    for entry in entries:
        path = _entry_path(entry)
        if path is None:
            violations.append(f"unrecognised extra_javascript entry: {entry!r}")
            continue
        if _LOCAL_PATH.fullmatch(path) and "//" not in path:
            continue
        parts = urlsplit(path)
        host = (parts.hostname or "").lower()
        if path.startswith("https://") and host in ALLOWED_HOSTS and "\\" not in path:
            continue
        violations.append(
            f"extra_javascript entry {path!r} is neither a plain local path nor an "
            "https URL on a host in ALLOWED_HOSTS; vendor it under "
            "docs/static/javascripts/"
        )
    return violations


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
