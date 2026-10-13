"""Render the e2e legs' preview links as a markdown section for the PR comment.

Each full-DAG e2e leg that passed writes ``results/e2e-preview/<Suite>.json``
(see ``BaseE2ETest._write_preview_links``): one product link per asset type the
run landed on its test tenant, uploaded as the ``e2e-preview-<app>-<leg>``
artifact. The Tests Gate downloads those and this script turns them into one
section of the sticky
"Tests Summary" comment, so a reviewer can open what the change published.

The files are written by code the pull request controls, so nothing in them is
trusted: a link is rendered only when it is an https URL of the exact
``/assets/<guid>/overview`` shape, a type or suite name only when it is a plain
identifier, and anything else is dropped rather than escaped.

Usage:
    python3 render_preview_links.py <artifacts-root>

Prints the markdown (empty when there is nothing to show) to stdout.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ARTIFACT_PREFIX = "e2e-preview-"
MAX_LINKS_PER_SUITE = 12

_LINK = re.compile(
    r"^https://[A-Za-z0-9.-]+(?::\d+)?/assets/[0-9A-Fa-f-]{36}/overview$"
)
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_EXPIRY = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$")


def _leg(artifact_dir: str) -> str:
    """The leg's label: its artifact directory without the shared prefix,
    e.g. ``atlan-example-app-example-e2e-aws``."""
    name = artifact_dir.removeprefix(ARTIFACT_PREFIX)
    return name if _NAME.match(name) else "e2e"


def collect(root: Path) -> list[tuple[str, str, list[tuple[str, str]], str | None]]:
    """(leg, suite, [(type, link)], expires_at) per preview file under *root*."""
    found = []
    for path in sorted(root.glob("*/*.json")):
        try:
            body = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(body, dict) or not isinstance(body.get("links"), dict):
            continue
        suite = str(body.get("suite", path.stem))
        if not _NAME.match(suite):
            suite = path.stem if _NAME.match(path.stem) else "suite"
        links = [
            (t, url)
            for t, url in sorted(body["links"].items())
            if isinstance(t, str)
            and isinstance(url, str)
            and _NAME.match(t)
            and _LINK.match(url)
        ][:MAX_LINKS_PER_SUITE]
        expires = body.get("expires_at")
        expires = (
            expires if isinstance(expires, str) and _EXPIRY.match(expires) else None
        )
        if links:
            found.append((_leg(path.parent.name), suite, links, expires))
    return found


def render(found: list[tuple[str, str, list[tuple[str, str]], str | None]]) -> str:
    if not found:
        return ""
    lines = [
        "### Preview: assets this run published",
        "",
        "| Leg | Suite | Asset type | Open |",
        "|---|---|---|---|",
    ]
    for leg, suite, links, _ in found:
        lines += [f"| {leg} | {suite} | {t} | [open]({url}) |" for t, url in links]
    expiries = sorted({e for *_, e in found if e})
    lines.append("")
    if expiries:
        lines.append(
            f"_CI test tenants, synthetic data; needs an Atlan login. Kept until "
            f"{expiries[0]}, then purged._"
        )
    else:
        lines.append(
            "_CI test tenants, synthetic data; needs an Atlan login. The run's "
            "cleanup purged these assets, so the links may already be dead._"
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: render_preview_links.py <artifacts-root>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    print(render(collect(root)) if root.is_dir() else "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
