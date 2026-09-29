#!/usr/bin/env python3
"""Find an app's hosted api member: the package behind an ``atlan.app_api`` entry point.

An app served by the consolidated API host keeps its one handler in a uv
workspace member (``api/``) that declares::

    [project.entry-points."atlan.app_api"]
    mysql = "atlan_mysql_api:handler"

Prints ``GITHUB_OUTPUT`` lines so ``tests-reusable.yaml`` can gate its api-member
leg with a job-level ``if`` (no conditional shell inlined in the workflow)::

    present=true
    member=api
    name=mysql
    package=atlan_mysql_api
    object=handler

``present=false`` (and nothing else) when the repo declares no such entry point;
exits 1 when it declares more than one, which the host cannot mount.

Usage:
    python3 detect_api_member.py [--root <repo root>]
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

GROUP = "atlan.app_api"
_SKIP_DIRS = frozenset({".venv", "node_modules", ".git", "tests", "build", "dist"})


@dataclass(frozen=True)
class ApiMember:
    member: str  # directory of the pyproject.toml, relative to the repo root
    name: str  # entry-point name == the app's Service name
    package: str  # top-level import package
    object: str  # attribute holding the handler


def _pyprojects(root: Path) -> list[Path]:
    found = []
    for path in sorted(root.rglob("pyproject.toml")):
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        found.append(path)
    return found


def find_members(root: Path) -> list[ApiMember]:
    """Every ``atlan.app_api`` entry point declared under ``root``."""
    members: list[ApiMember] = []
    for path in _pyprojects(root):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        eps = data.get("project", {}).get("entry-points", {}).get(GROUP, {})
        for name, target in sorted(eps.items()):
            module, _, obj = str(target).partition(":")
            members.append(
                ApiMember(
                    member=str(path.parent.relative_to(root)) or ".",
                    name=name,
                    package=module.split(".", 1)[0],
                    object=obj or "handler",
                )
            )
    return members


def outputs(members: list[ApiMember]) -> list[str]:
    """The ``GITHUB_OUTPUT`` lines for ``members`` (raises on more than one)."""
    if not members:
        return ["present=false"]
    if len(members) > 1:
        raise ValueError(
            f"more than one {GROUP} entry point declared: "
            + ", ".join(f"{m.name} ({m.member})" for m in members)
        )
    m = members[0]
    return [
        "present=true",
        f"member={m.member}",
        f"name={m.name}",
        f"package={m.package}",
        f"object={m.object}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        lines = outputs(find_members(args.root))
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
