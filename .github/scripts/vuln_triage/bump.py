"""The Case-1 bump: a constraint floor in pyproject.toml and a targeted uv.lock upgrade.

This follows the repo's own pattern. `[tool.uv] constraint-dependencies` already carries
one `"pkg>=fixed",  # CVE-...` line per past fix, so a later `uv lock` can never
silently resolve back below the fix. Only the affected packages are upgraded
(`uv lock --upgrade-package`). A blanket `--upgrade` would also pull in unrelated
releases that are too new for the org's 7-day cooldown.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .classify import version_key
from .scan import lock_registry_hosts, normalize

_BLOCK = re.compile(r"^constraint-dependencies = \[\n(?P<body>.*?)^\]", re.M | re.S)
_ENTRY = re.compile(
    r'^(?P<indent>\s*)"(?P<name>[A-Za-z0-9_.\-]+)(?P<spec>[^"]*)",(?P<rest>.*)$'
)


class BumpError(RuntimeError):
    """The bump cannot be planned or did not do what it must."""


def set_constraints(pyproject: str, floors: dict[str, tuple[str, list[str]]]) -> str:
    """Add or raise `"pkg>=version",  # CVE-...` lines in `constraint-dependencies`.

    `floors` maps package -> (minimum version, CVE ids). An existing entry is rewritten
    only when the new floor is higher; its trailing comment keeps its CVEs and gains ours.
    """
    m = _BLOCK.search(pyproject)
    if not m:
        raise BumpError("pyproject.toml has no [tool.uv] constraint-dependencies block")
    lines = m.group("body").splitlines()
    pending = {normalize(k): (k, v) for k, v in floors.items()}
    for i, line in enumerate(lines):
        e = _ENTRY.match(line)
        if not e or normalize(e.group("name")) not in pending:
            continue
        name, (version, cves) = pending.pop(normalize(e.group("name")))
        current = re.search(r">=\s*([^,;\s]+)", e.group("spec"))
        if current and version_key(current.group(1)) >= version_key(version):
            continue
        comment = e.group("rest").strip().lstrip("#").strip()
        tags = ", ".join([c for c in [comment] if c] + cves)
        lines[i] = f'{e.group("indent")}"{e.group("name")}>={version}",  # {tags}'
    for name, (version, cves) in pending.values():
        lines.append(f'    "{name}>={version}",  # {", ".join(cves)}')
    body = "\n".join(lines) + "\n"
    return pyproject[: m.start("body")] + body + pyproject[m.end("body") :]


def verify(
    *,
    old_lock: dict[str, dict],
    new_lock: dict[str, dict],
    old_text: str,
    new_text: str,
    targets: dict[str, str],
    now: datetime,
    cooldown_days: int,
) -> tuple[list[str], list[str]]:
    """Check the regenerated lock. Returns (errors, fresh).

    errors: anything that means the bump must not be opened. A target that did not move
    or landed below its fix, a registry host the old lock never used (the Endor-firewall
    rewrite trap), or a lost `revision` header.
    fresh:  targets whose chosen release is younger than the cooldown. The PR still opens,
    because the release patches the CVE, but without the auto-merge label so a human
    reviews it.
    """
    errors: list[str] = []
    fresh: list[str] = []
    if not re.search(r"^revision = ", new_text, re.M):
        errors.append("uv.lock lost its `revision` header")
    new_hosts = lock_registry_hosts(new_text) - lock_registry_hosts(old_text)
    if new_hosts:
        errors.append(
            f"uv.lock now points at new host(s): {', '.join(sorted(new_hosts))}"
        )
    for pkg, target in targets.items():
        key = normalize(pkg)
        old = old_lock.get(key, {}).get("version", "")
        new = new_lock.get(key, {})
        got = new.get("version", "")
        if not got:
            errors.append(f"{pkg} disappeared from uv.lock")
            continue
        if got == old or version_key(got) < version_key(target):
            errors.append(f"{pkg} resolved to {got}, below the fix {target}")
            continue
        upload = new.get("upload_time", "")
        if upload:
            ts = datetime.fromisoformat(upload.replace("Z", "+00:00"))
            if now - ts < timedelta(days=cooldown_days):
                fresh.append(f"{pkg} {got} (published {ts.date().isoformat()})")
    return errors, fresh
