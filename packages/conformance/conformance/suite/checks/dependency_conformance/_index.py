"""D012 / D013 / D014 — the default package index, ``uv.lock`` artifact hosts and the resolver fence."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from conformance.suite.checks._ast_common import (
    parse_toml_suppressions,
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

from ._common import (
    _LOCK_CREDENTIAL_MARKERS,
    _LOCK_URL_RE,
    PYPI_LOCK_HOSTS,
    PYPI_SIMPLE_URL,
    RULE_D012,
    RULE_D013,
    RULE_D014,
    _line_of,
    _make_finding,
    _safe_load,
)


def _table_header_line(text: str, name: str) -> int | None:
    """Return the 1-based line of the ``[name]`` or ``[[name]]`` header, else None."""
    pat = re.compile(rf"^\s*\[{{1,2}}\s*{re.escape(name)}\s*\]{{1,2}}\s*(?:#.*)?$")
    for ln, line in enumerate(text.splitlines(), start=1):
        if pat.match(line):
            return ln
    return None


def _uv_index_tables(text: str) -> list[tuple[int, dict[str, int]]]:
    """Return one ``(header_line, {key: line})`` pair per ``[[tool.uv.index]]``.

    :func:`_line_of` cannot be reused for this: its table pattern matches a
    single-bracket ``[table]`` header only, so an *array*-of-tables header
    never registers and every lookup silently falls through to line 1.  That
    would anchor every D012/D013 finding at the top of the file and, worse,
    put the only working suppression line somewhere no reader would think to
    write it — the directive has to sit on the finding's line or the one above.
    """
    header_re = re.compile(r"^\s*\[\[\s*tool\.uv\.index\s*\]\]")
    any_table_re = re.compile(r"^\s*\[{1,2}[^\]]+\]{1,2}\s*(?:#.*)?$")
    key_re = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")

    tables: list[tuple[int, dict[str, int]]] = []
    current: dict[str, int] | None = None
    for ln, line in enumerate(text.splitlines(), start=1):
        if header_re.match(line):
            current = {}
            tables.append((ln, current))
            continue
        if any_table_re.match(line):
            current = None
            continue
        if current is None:
            continue
        m = key_re.match(line)
        if m is not None:
            current.setdefault(m.group(1), ln)
    return tables


def _uv_config_anchor(text: str) -> int:
    """Line to anchor a D012/D013 finding on when no default index is declared.

    The first ``[[tool.uv.index]]`` header if the repo has any index tables at
    all, else the ``[tool.uv]`` header, else line 1.  Whatever this returns is
    also where an inline ``# conformance: ignore[...]`` has to be written, so
    it deliberately points at uv's own configuration rather than at the top of
    the file.
    """
    tables = _uv_index_tables(text)
    if tables:
        return tables[0][0]
    return _table_header_line(text, "tool.uv") or 1


def _default_index(
    text: str, *, data: Mapping[str, Any] | None = None
) -> tuple[str, int] | None:
    """Return ``(url, lineno)`` of the ``[[tool.uv.index]]`` marked default.

    ``None`` when no entry is marked ``default = true`` — which is D012's
    first branch, not an error.  When several are marked, the first is what is
    graded, because that is the one uv uses.  *lineno* is the entry's ``url =``
    line, falling back to its table header.
    """
    if data is None:
        data = _safe_load(text)
    if data is None:
        return None
    tool = data.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    indexes = uv.get("index") if isinstance(uv, dict) else None
    if not isinstance(indexes, list):
        return None

    tables = _uv_index_tables(text)
    for position, entry in enumerate(indexes):
        if not isinstance(entry, dict) or entry.get("default") is not True:
            continue
        url = entry.get("url")
        if not isinstance(url, str):
            continue
        # The parsed array and the textual tables are in the same order, so the
        # nth entry is the nth header; fall back to the file's uv anchor if the
        # two ever disagree (a table written inline, say) rather than guessing.
        if position < len(tables):
            header_line, keys = tables[position]
            return url, keys.get("url", header_line)
        return url, _uv_config_anchor(text)
    return None


def _scan_default_index(text: str, rel_pyproject: str) -> list[Finding]:
    """D012: the root pyproject must pin PyPI as uv's default index.

    Repo-level and at most one finding: this is a property of the repo's
    resolution, not of each sub-package, so a monorepo does not collect one
    finding per member.  Two branches — no default index at all, or a default
    index that is not PyPI (the proxy rewrite already committed).
    """
    suppressions = parse_toml_suppressions(text)
    canonical = (
        f'[[tool.uv.index]]\nname = "pypi"\nurl = "{PYPI_SIMPLE_URL}"\ndefault = true'
    )
    default = _default_index(text)

    if default is None:
        return [
            _make_finding(
                rule_id=RULE_D012,
                file=rel_pyproject,
                line=_uv_config_anchor(text),
                column=1,
                message=(
                    "pyproject.toml declares no [[tool.uv.index]] entry marked "
                    "'default = true', so the resolver inherits whatever default "
                    "index the machine supplies. On an Atlan laptop that is the "
                    "Endor Labs package firewall in ~/.config/uv/uv.toml, and "
                    "every 'uv run'/'uv sync'/'uv lock' then rewrites every "
                    "package URL in uv.lock to that credentialed proxy — "
                    "silently, with no version or hash change. Committing that "
                    "lock makes CI fail at dependency install with 401 "
                    f"Unauthorized. Add:\n{canonical}\nPut it in pyproject.toml, "
                    "not in a project-level uv.toml: a uv.toml suppresses "
                    "[tool.uv] here entirely and would silently drop every "
                    "setting declared in it."
                ),
                suppressions=suppressions,
            )
        ]

    url, lineno = default
    if url.rstrip("/") != PYPI_SIMPLE_URL.rstrip("/"):
        return [
            _make_finding(
                rule_id=RULE_D012,
                file=rel_pyproject,
                line=lineno,
                column=1,
                message=(
                    f"The default [[tool.uv.index]] resolves from "
                    f"'{_host_of(url)}', not from PyPI. Every 'uv lock' then "
                    f"records that host in uv.lock, and CI — which holds no "
                    f"credential for an internal proxy — fails at dependency "
                    f"install with 401 Unauthorized. Set url = "
                    f"'{PYPI_SIMPLE_URL}' (an internal mirror that is genuinely "
                    f"required should be a non-default index, or an explicit "
                    f"inline suppression with the reason)."
                ),
                suppressions=suppressions,
            )
        ]

    return []


#: An ``exclude-newer`` value that is a point in time rather than a duration.
#: uv accepts both an RFC 3339 timestamp and a bare ``YYYY-MM-DD``; either is a
#: fence that never moves.  A duration (``P3D``, ``7 days``) genuinely rolls and
#: is not this rule's concern — which is why the test is on the VALUE's shape
#: and not on the key's presence.
_ABSOLUTE_DATE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")


def _absolute_fences(
    text: str, *, data: Mapping[str, Any] | None = None
) -> list[tuple[str, str, int, str | None]]:
    """Return ``(label, value, lineno, discriminator)`` per absolute fence.

    Two places carry one, and a check that reads only the first misreports the
    second: ``exclude-newer`` fences the whole repo, while
    ``exclude-newer-package`` fences named packages — atlanhq/atlan-mongodbatlas-app
    carries both at once, with the repo-wide value three weeks older than the
    per-package carve-outs written to work around it.

    Every per-package fence carries a *discriminator*.  Written as an inline
    table they all share one line, so without it two fenced packages hash to
    one SARIF identity and a suppression on that line silences both — the
    discriminator is what keeps them addressable.  Written as a
    ``[tool.uv.exclude-newer-package]`` sub-table each key has a line of its
    own, and the anchor is that line, so a comment sits where the reader
    expects; the discriminator is still set, because the fingerprint must not
    depend on which of the two spellings the repo happens to use.
    """
    if data is None:
        data = _safe_load(text)
    if data is None:
        return []
    tool = data.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    if not isinstance(uv, dict):
        return []

    anchor = _line_of(text, "exclude-newer", section="tool.uv")
    found: list[tuple[str, str, int, str | None]] = []

    whole = uv.get("exclude-newer")
    if isinstance(whole, str) and _ABSOLUTE_DATE.match(whole):
        found.append(("exclude-newer", whole, anchor, None))

    per_package = uv.get("exclude-newer-package")
    if isinstance(per_package, dict):
        inline_anchor = _line_of(text, "exclude-newer-package", section="tool.uv")
        for name, value in sorted(per_package.items()):
            if not (isinstance(value, str) and _ABSOLUTE_DATE.match(value)):
                continue
            label = f"exclude-newer-package.{name}"
            # A sub-table gives each package its own line; an inline table does
            # not, and _line_of returns 1 when it finds nothing — which would
            # anchor the finding at the top of the file, where no reader would
            # think to write the directive. So take the key's own line only
            # when one was actually found.
            key_line = _line_of(text, name, section="tool.uv.exclude-newer-package")
            found.append(
                (label, value, key_line if key_line > 1 else inline_anchor, label)
            )
    return found


def _scan_resolver_fence(text: str, rel_pyproject: str) -> list[Finding]:
    """D014: no absolute-dated ``[tool.uv] exclude-newer`` in the root pyproject.

    One finding per fence, so a repo that carves out individual packages is told
    about each rather than only the repo-wide one.

    The message names the date but never how stale it is. The staleness is the
    whole point of the rule, and it is also the one fact that would change on
    every run — a day count in the message would rewrite the SARIF, move the
    fingerprint and re-notify on an unchanged repo, every day, forever. The
    reader can subtract.

    Per-package fences carry their label as the finding's discriminator, so
    each keeps a distinct fingerprint and can be suppressed alone with
    ``# conformance: ignore[D014:exclude-newer-package.<name>]``.
    """
    suppressions = parse_toml_suppressions(text)
    findings: list[Finding] = []
    for label, value, lineno, discriminator in _absolute_fences(text):
        findings.append(
            _make_finding(
                rule_id=RULE_D014,
                file=rel_pyproject,
                line=lineno,
                column=1,
                discriminator=discriminator,
                message=(
                    f"[tool.uv] {label} is pinned to the fixed date '{value}', "
                    "which is a freeze rather than a cooldown: it does not roll "
                    "forward, so the window it enforces widens by a day every "
                    "day and nothing fails when nobody moves it. It bounds EVERY "
                    "resolve in the repo — including /fix-vulnerabilities, which "
                    "is how a CVE fix actually lands — and it is silent: Renovate "
                    "still opens upgrade PRs, because its package datasource is "
                    "unbounded, and then 'uv lock --upgrade-package' returns the "
                    "lock unchanged because uv cannot see past the fence. Delete "
                    "the key and rely on the fleet's rolling release-age cooldown "
                    "(renovate-config/default.json applies minimumReleaseAge "
                    "centrally, and the lock lane is bounded in postUpgradeTasks). "
                    "If this repo genuinely needs its own hold, use a duration — "
                    "'exclude-newer-span' — so it rolls, or suppress inline with "
                    "the reason and an owner."
                ),
                suppressions=suppressions,
            )
        )
    return findings


def _host_of(url: str) -> str:
    """Return the hostname of *url*, or a placeholder when it does not parse.

    Only ever the *host* — never the full URL. D013 findings reach SARIF,
    GitHub code scanning, CI logs and the remediation run artifacts, so a URL
    carrying an index credential must not be interpolated into a message.
    """
    try:
        return urlsplit(url).hostname or "<unparseable>"
    except ValueError:
        return "<unparseable>"


def _scan_lockfile_index(root: Path, text: str, rel_pyproject: str) -> list[Finding]:
    """D013: no non-PyPI host, and no credential, in the committed uv.lock.

    Anchored on ``pyproject.toml`` rather than on ``uv.lock``: a lockfile is
    regenerated wholesale on every lock, so a ``# conformance: ignore[D013]``
    directive written into it would not survive, leaving the rule with no
    suppression path.  ``D011``'s lock branch sets the same precedent.

    A missing or unparseable lock never manufactures a finding.  A lock that
    parses but yields *no* URLs is reported to stderr as undetermined rather
    than passing silently — the URL spelling is a uv implementation detail,
    and a matcher that has gone inert must not read as a clean result.
    """
    lock = root / "uv.lock"
    lock_text = safe_read_text(lock)
    if lock_text is None:
        return []

    urls = _LOCK_URL_RE.findall(lock_text)
    if not urls:
        print(  # noqa: T201 — stderr diagnostic; stdout carries the SARIF report
            "conformance (D013): found no download URLs in uv.lock "
            "(non-PyPI index status undetermined). Either the lock declares no "
            "registry packages or its URL format changed and this check is now "
            "inert — fix the check rather than removing it.",
            file=sys.stderr,
        )
        return []

    suppressions = parse_toml_suppressions(text)
    anchor = _uv_config_anchor(text)

    # ── Branch 1: a credential in a committed file ──────────────────────────
    # Reported first and alone: a proxy host in the lock is a broken build, a
    # credential in the lock is a secret in version control.  Neither the URL
    # nor the userinfo is echoed into the message.
    credentialed = sum(
        1
        for url in urls
        if _credentialed(url) or any(m in url for m in _LOCK_CREDENTIAL_MARKERS)
    )
    if credentialed:
        noun = "URL that embeds" if credentialed == 1 else "URLs that embed"
        return [
            _make_finding(
                rule_id=RULE_D013,
                file=rel_pyproject,
                line=anchor,
                column=1,
                message=(
                    f"uv.lock contains {credentialed} {noun} an index "
                    f"credential. This is a secret in version control: rotate the "
                    f"credential before doing anything else, then regenerate the "
                    f"lock against PyPI. The offending values are deliberately "
                    f"not reproduced here — read them from uv.lock directly."
                ),
                suppressions=suppressions,
            )
        ]

    # ── Branch 2: a non-PyPI host ───────────────────────────────────────────
    offenders: dict[str, int] = {}
    for url in urls:
        host = _host_of(url)
        if host not in PYPI_LOCK_HOSTS:
            offenders[host] = offenders.get(host, 0) + 1
    if not offenders:
        return []

    summary = ", ".join(
        f"{host} ({count})" for host, count in sorted(offenders.items())
    )
    return [
        _make_finding(
            rule_id=RULE_D013,
            file=rel_pyproject,
            line=anchor,
            column=1,
            message=(
                f"uv.lock resolves packages from a host that is not PyPI: "
                f"{summary}. This is almost always the package-firewall rewrite "
                f"from a local 'uv run'/'uv sync' on a machine with a "
                f"machine-wide default index; it changes no version and no hash, "
                f"only the URLs. CI holds no credential for that host and fails "
                f"at dependency install with 401 Unauthorized, on a different "
                f"package each run. Restore the committed lock from version "
                f"control; if a dependency genuinely changed, re-lock with "
                f"'uv lock --default-index {PYPI_SIMPLE_URL}'. Pin the index in "
                f"pyproject.toml (D012) so it does not recur."
            ),
            suppressions=suppressions,
        )
    ]


def _credentialed(url: str) -> bool:
    """True iff *url* carries userinfo (``user[:pass]@host``)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(parts.username or parts.password)
