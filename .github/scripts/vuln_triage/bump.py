"""The Case-1 bump: a constraint floor in pyproject.toml and a targeted uv.lock upgrade.

This follows the repo's own pattern. `[tool.uv] constraint-dependencies` already carries
one `"pkg>=fixed",  # CVE-...` line per past fix, so a later `uv lock` can never
silently resolve back below the fix. A constraint pins this repo's lock only, so when
the package is also a published `[project] dependencies` entry its floor is raised there
too: that is the floor connector apps inherit. A runtime package that is not published
yet is left for a human (a new direct dependency needs approval), and
tests/unit/test_security_floors.py fails the PR until it is. Only the affected packages
are upgraded
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
# The `[project]` table's `dependencies = [...]`: the published requirements.
_PROJECT = re.compile(r"^\[project\]\n(?P<table>.*?)(?=^\[|\Z)", re.M | re.S)
_DEPS = re.compile(r"^dependencies = \[\n(?P<body>.*?)^\]", re.M | re.S)
_PUBLISHED = re.compile(
    r'^(?P<head>\s*"(?P<name>[A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?)(?P<spec>[^";]*)'
    r'(?P<tail>[^"]*",)(?P<rest>.*)$'
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
    out = pyproject[: m.start("body")] + body + pyproject[m.end("body") :]
    return _raise_published(out, floors)


def _tag(rest: str, cves: list[str]) -> str:
    """`rest` (what follows the entry's comma) with `cves` added to its comment."""
    code, _, comment = rest.partition("#")
    tags = ", ".join([c for c in [comment.strip()] if c] + cves)
    return f"{code.rstrip()}  # {tags}" if tags else code.rstrip()


def _raise_published(pyproject: str, floors: dict[str, tuple[str, list[str]]]) -> str:
    """Raise the `>=` floor of each published `[project] dependencies` entry in `floors`.

    The upper bound, extras and marker are kept; an entry already at or above the floor
    is untouched. An entry pinned another way (`==`, `~=`) is left alone: rewriting it
    would change more than the floor, so the test fails the PR for a human to decide.
    """
    project = _PROJECT.search(pyproject)
    deps = _DEPS.search(project.group("table")) if project else None
    if not project or not deps:
        return pyproject
    wanted = {normalize(k): v for k, v in floors.items()}
    lines = deps.group("body").splitlines()
    for i, line in enumerate(lines):
        e = _PUBLISHED.match(line)
        if not e or normalize(e.group("name")) not in wanted:
            continue
        version, cves = wanted[normalize(e.group("name"))]
        spec = e.group("spec")
        if re.search(r"(==|~=)", spec):
            continue
        current = re.search(r">=\s*([^,;\s]+)", spec)
        if current and version_key(current.group(1)) >= version_key(version):
            continue
        if current:
            spec = spec[: current.start(1)] + version + spec[current.end(1) :]
        else:
            spec = f">={version}" + (f",{spec}" if spec.strip() else "")
        lines[i] = (
            e.group("head") + spec + e.group("tail") + _tag(e.group("rest"), cves)
        )
    start = project.start("table") + deps.start("body")
    end = project.start("table") + deps.end("body")
    return pyproject[:start] + "\n".join(lines) + "\n" + pyproject[end:]


def verify(
    *,
    old_lock: dict[str, dict[str, str]],
    new_lock: dict[str, dict[str, str]],
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
    errors = _shape_errors("uv.lock", old_text, new_text)
    fresh: list[str] = []
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


def _shape_errors(label: str, old_text: str, new_text: str) -> list[str]:
    """A lost `revision` header or a registry host the old lock never used."""
    errors: list[str] = []
    if not re.search(r"^revision = ", new_text, re.M):
        errors.append(f"{label} lost its `revision` header")
    new_hosts = lock_registry_hosts(new_text) - lock_registry_hosts(old_text)
    if new_hosts:
        errors.append(
            f"{label} now points at new host(s): {', '.join(sorted(new_hosts))}"
        )
    return errors


def verify_follower(
    *,
    label: str,
    old_lock: dict[str, dict[str, str]],
    new_lock: dict[str, dict[str, str]],
    old_text: str,
    new_text: str,
    targets: dict[str, str],
    now: datetime,
    cooldown_days: int,
) -> tuple[list[str], list[str]]:
    """`verify` for a lock relocked alongside the root one (FND-4716).

    packages/conformance/uv.lock path-sources the SDK, so it records the SDK's
    dependencies: a raised `[project] dependencies` floor left unrelocked there
    fails the PR's conformance lock check and, if it ever merged, the next
    conformance release. Unlike the root lock, a target need not be in this lock's
    closure: an absent one is not checked. One present must reach its fix, and a
    release it moved to inside the cooldown is `fresh`, as for the root lock.
    """
    errors = _shape_errors(label, old_text, new_text)
    fresh: list[str] = []
    for pkg, target in targets.items():
        key = normalize(pkg)
        old = old_lock.get(key, {}).get("version", "")
        new = new_lock.get(key, {})
        got = new.get("version", "")
        if not got:
            if old:
                errors.append(f"{pkg} disappeared from {label}")
            continue
        if version_key(got) < version_key(target):
            errors.append(f"{pkg} resolved to {got} in {label}, below the fix {target}")
            continue
        upload = new.get("upload_time", "")
        if got != old and upload:
            ts = datetime.fromisoformat(upload.replace("Z", "+00:00"))
            if now - ts < timedelta(days=cooldown_days):
                fresh.append(
                    f"{pkg} {got} in {label} (published {ts.date().isoformat()})"
                )
    return errors, fresh
