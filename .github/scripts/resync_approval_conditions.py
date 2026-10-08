#!/usr/bin/env python3
"""Approval gate for conformance-resync PRs — the atlan-ci code-owner review.

The conformance resync lane (``.github/workflows/conformance-resync.yml``, FND-2868)
keeps one PR per app repo on ``bot/conformance-resync``, authored by the
atlan-conformance-sync App, carrying ``atlan-application-sdk-conformance bootstrap --resync`` output:
the re-rendered tests.yaml / renovate.json / managed workflow shims / review
kit. Those PRs rewrite ``.github/workflows/*`` files, so the Renovate gate in
``renovate_approval_conditions.py`` correctly refuses them (pin-only workflow
diffs, FND-1996). This module is the separate, narrower path for them.

**Identity selects; content proves.** Author and branch only decide which PRs
reach this gate — a branch name is something anyone with push access can create, so neither is
trusted as evidence. The approval rests on re-rendering the PR independently
here and requiring a byte-identical result:

  a. author is ``atlan-conformance-sync[bot]``, head branch is exactly
     ``bot/conformance-resync`` in this same repo, base is ``main``, PR open
     and not a draft,
     current HEAD is the SHA under evaluation
  b. the body carries the lane's marker, naming the suite version it rendered
  c. exactly one commit on the PR, authored by the lane, with one parent
  c0. the marker's ``resolved-at`` is a real UTC date no later than that
      commit's committer date or now. It sets the release-age fence and the
      body is editable by anyone with write access, so a later date would
      lift the fence
  d. that parent is in the base branch's history (the render base is real main)
  e0. ``renovate.json`` at that parent is in auto-merge mode
     (``discover_org_consumers.automerge_mode`` == ``auto``); soft, unknown,
     missing and unreadable withhold the approval, so a person reviews resync
     PRs in soft-mode repos
  e. re-render: check out the parent, read the conformance version its
     ``uv.lock`` resolves (must equal the marker), run ``bootstrap --resync
     --json`` at exactly that version with the marker's ``resolved-at``
     resolution fence (:func:`resync_command`) inside the pinned container
     (:func:`sandboxed_render` — the render is third-party code and this
     process holds the atlan-ci PAT), stage exactly what the lane stages, and
     require the resulting git tree to EQUAL the PR head's tree — any extra,
     missing or altered byte anywhere withholds the approval
  f. the re-render dropped no per-repo setting (``.bak`` set-compare)
  g. every ruleset-required check is green
  h. atlan-ci has not already approved this head with the resync signature;
     the head is re-read just before posting, and the review is pinned to it
     with ``commit_id``

(g) and (h) are evaluated before (e): they cost one API call each, the render
costs an image pull and a package install.

The lane (``.github/scripts/conformance_resync.py``) imports
:func:`stage_like_the_lane`, :data:`ACCEPTED_DROPS` and the marker from here, so
the two cannot drift; if they ever did, the trees would differ, which fails
CLOSED (no approval), never open.

Fail closed throughout: anything other than an affirmative signal skips.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import discover_org_consumers as discover

# Constants the resync lane (conformance_resync.py) shares with this gate.
RESYNC_AUTHOR = "atlan-conformance-sync[bot]"
RESYNC_BRANCH = "bot/conformance-resync"
RESYNC_SIGNATURE = "**Conformance resync auto-approval:**"
APPROVER_LOGIN = "atlan-ci"
CONFORMANCE_PACKAGE = "atlan-application-sdk-conformance"
_MARKER_RE = re.compile(
    r"<!--\s*conformance-resync-lane\s+suite=(\d+\.\d+\.\d+)"
    r"\s+resolved-at=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*-->"
)
BASE_BRANCH = "main"
RELEASE_AGE = timedelta(days=7)
FIRST_PARTY = (
    "atlan-application-sdk",
    "atlan-application-sdk-conformance",
    "pyatlan",
)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
# Well inside the approver job's 10-minute limit (pull + install + render),
# so a slow render fails closed with a message instead of a killed job.
BOOTSTRAP_TIMEOUT = 300
# Every gh/git call is bounded, so a stalled request fails the step in
# seconds rather than holding the job to its workflow timeout.
GH_TIMEOUT = 60
GIT_TIMEOUT = 300


def pr_marker(suite_version: str, resolved_at: str) -> str:
    """The hidden marker every lane PR body leads with. Shared with the lane
    (``conformance_resync.py``) so the two never render it differently.
    ``resolved_at`` fixes the dependency resolution both sides use."""
    return (
        f"<!-- conformance-resync-lane suite={suite_version} "
        f"resolved-at={resolved_at} -->"
    )


_RESOLVED_AT_FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_resolved_at(text: str | None) -> datetime | None:
    """``resolved-at`` as an aware UTC datetime, or ``None`` when absent or not
    a real date (the marker regex accepts month 13; ``strptime`` does not)."""
    if not text:
        return None
    try:
        return datetime.strptime(text, _RESOLVED_AT_FMT).replace(tzinfo=UTC)
    except ValueError:
        return None


def check_resolved_at(text: str | None, *caps: datetime) -> tuple[bool, str]:
    """Whether ``resolved-at`` is a real date no later than every cap.

    ``resolved-at`` sets the third-party release-age fence, and it lives in the
    PR body, which anyone with write access to the app repo can edit. A date in
    the future would lift the fence. A date at or before the caps keeps every
    third-party package at least :data:`RELEASE_AGE` old when it is used, which
    is all the cooldown asks. The lane always writes a time it has already
    reached, before it commits, so its own values pass.
    """
    at = parse_resolved_at(text)
    if at is None:
        return False, f"resolved-at {text!r} is not a valid UTC timestamp"
    for cap in caps:
        if at > cap:
            return False, (
                f"resolved-at {text} is later than {cap.strftime(_RESOLVED_AT_FMT)} "
                "— it would lift the release-age fence"
            )
    return True, ""


def resync_command(suite: str, resolved_at: str) -> list[str]:
    """The one ``bootstrap --resync`` invocation the lane and this gate run.
    Third-party packages resolve as of ``resolved_at`` minus the org release-age
    window; first-party packages as of ``resolved_at`` itself."""
    at = datetime.strptime(resolved_at, _RESOLVED_AT_FMT)
    cutoff = (at - RELEASE_AGE).strftime(_RESOLVED_AT_FMT)
    first_party = [
        arg
        for pkg in FIRST_PARTY
        for arg in ("--exclude-newer-package", f"{pkg}={resolved_at}")
    ]
    return [
        "uvx",
        "--isolated",
        "--no-config",
        "--exclude-newer",
        cutoff,
        *first_party,
        "--from",
        f"{CONFORMANCE_PACKAGE}=={suite}",
        CONFORMANCE_PACKAGE,
        "bootstrap",
        "--resync",
        "--json",
    ]


# The render runs third-party PyPI code, so it never runs on the host. Same
# uv as setup-uv pins in the workflows; bump both together.
RENDER_IMAGE = (
    "ghcr.io/astral-sh/uv:0.12.18-python3.12-trixie-slim"
    "@sha256:38f41574703989d6e5f02be80a3d687b00f98744cce86908097bcd34bcb7eb98"
)


def render_argv(
    scratch: str, suite: str, resolved_at: str, name: str, uid: int, gid: int
) -> list[str]:
    """``docker run`` for :func:`resync_command`, with nothing from the host
    but the scratch copy of the tree.

    On a hosted runner the host user has passwordless sudo and the runner
    process holds every secret the job references, so dropping ``GH_TOKEN``
    from a child's env protects nothing: a same-host child can read the
    parent's ``/proc/<pid>/environ`` or the runner's memory. A container has
    its own PID namespace, gets no host env, and sees only ``/w``.
    """
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--user",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,exec,size=2g",
        "-e",
        "HOME=/tmp",
        "-e",
        "UV_CACHE_DIR=/tmp/uv-cache",
        "-e",
        "UV_PYTHON_DOWNLOADS=never",
        "-v",
        f"{scratch}:/w",
        "-w",
        "/w",
        RENDER_IMAGE,
        *resync_command(suite, resolved_at),
    ]


def _unsafe_rel(path: str) -> bool:
    parts = pathlib.PurePosixPath(path).parts
    return (
        not path
        or path.startswith("/")
        or ".." in parts
        or not parts
        or parts[0] == ".git"
    )


def _via_symlink(root: pathlib.Path, rel: str) -> bool:
    cur = root
    for part in pathlib.PurePosixPath(rel).parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def copy_back(scratch: pathlib.Path, work: pathlib.Path, manifest: dict) -> list[str]:
    """Bring the render's output from ``scratch`` into the trusted clone.

    Only the manifest's ``touched`` paths and the ``.bak`` backups come back,
    as regular files with their exec bit; a path the render deleted is
    deleted here. Nothing under ``.git`` ever comes back: a render able to
    write ``.git/config`` or a hook would run code at the next host-side
    ``git`` call, which holds the token. A path under a directory the
    clone already has as a symlink is skipped, as the lane's staging skips
    it; a symlinked directory only the render has is refused. Returns the
    refused paths; any refusal fails the render closed.
    """
    wanted = {p for p in manifest.get("touched") or [] if isinstance(p, str)}
    for bak in scratch.rglob("*.bak"):
        rel = bak.relative_to(scratch)
        if rel.parts and rel.parts[0] != ".git":
            wanted.add(rel.as_posix())
    refused: list[str] = []
    for rel in sorted(wanted):
        src, dest = scratch / rel, work / rel
        if _unsafe_rel(rel):
            refused.append(rel)
            continue
        if _via_symlink(work, rel):
            continue
        if _via_symlink(scratch, rel):
            refused.append(rel)
            continue
        if src.is_symlink() or (src.exists() and not src.is_file()):
            refused.append(rel)
            continue
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.exists():
            refused.append(rel)
            continue
        if not src.exists():
            continue  # the render deleted it
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        dest.chmod(0o755 if src.stat().st_mode & 0o111 else 0o644)
    return refused


def sandboxed_render(
    work: str, suite: str, resolved_at: str, runner: Runner
) -> tuple[int, str, str]:
    """Run :func:`resync_command` against a copy of ``work`` (without
    ``.git``) inside :data:`RENDER_IMAGE`, then :func:`copy_back` its output.
    ``(returncode, stdout, stderr)``; the lane and this gate both use it."""
    name = f"resync-render-{os.urandom(6).hex()}"
    with tempfile.TemporaryDirectory(prefix="resync-render-") as tmp:
        scratch = pathlib.Path(tmp, "w")
        shutil.copytree(
            work,
            scratch,
            symlinks=True,
            ignore=lambda d, names: [".git"]
            if pathlib.Path(d) == pathlib.Path(work)
            else [],
        )
        try:
            proc = runner(
                render_argv(
                    str(scratch), suite, resolved_at, name, os.getuid(), os.getgid()
                ),
                capture_output=True,
                text=True,
                timeout=BOOTSTRAP_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            runner(
                ["docker", "rm", "-f", name],
                capture_output=True,
                check=False,
                timeout=GH_TIMEOUT,
            )
            return 124, "", f"render timed out after {BOOTSTRAP_TIMEOUT}s"
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        manifest = parse_manifest(stdout)
        if proc.returncode != 0 or manifest is None or manifest.get("skipped"):
            return proc.returncode, stdout, stderr
        refused = copy_back(scratch, pathlib.Path(work), manifest)
        if refused:
            return (
                1,
                stdout,
                f"render wrote paths that are never copied back: {refused}",
            )
        return 0, stdout, stderr


RESYNC_APPROVAL_BODY = (
    f"{RESYNC_SIGNATURE} this PR's tree is byte-identical to an independent "
    "`bootstrap --resync` render of its base commit at the conformance version "
    "that commit's `uv.lock` pins, it is a single commit by the resync lane, it "
    "drops no per-repo setting, and every required check is green.\n\n"
    "Posted by `.github/scripts/resync_approval_conditions.py` "
    "(application-sdk). Any push to this branch dismisses this approval."
)

Runner = Callable[..., subprocess.CompletedProcess]


class GhError(RuntimeError):
    """A GitHub or git call failed — aborts the step (a red step is visible)."""


def call(
    runner: Runner, cmd: list[str], *, timeout: int = GH_TIMEOUT, **kwargs: Any
) -> subprocess.CompletedProcess:
    """``runner(cmd)`` with output captured and a bounded ``timeout``; a call
    that overruns raises :class:`GhError` instead of hanging."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", False)
    try:
        return runner(cmd, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise GhError(
            f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} timed out after {timeout}s"
        ) from exc


def _is_not_found(result: subprocess.CompletedProcess) -> bool:
    return "HTTP 404" in (result.stderr or "") or "Not Found" in (result.stderr or "")


# ---------------------------------------------------------------------------
# Pure conditions
# ---------------------------------------------------------------------------


def is_candidate(meta: dict[str, Any]) -> bool:
    """Whether this PR belongs to the resync path at all (routing only — not
    evidence; see the module docstring)."""
    return ((meta.get("user") or {}).get("login")) == RESYNC_AUTHOR and (
        (meta.get("head") or {}).get("ref")
    ) == RESYNC_BRANCH


def marker_suite_version(body: str | None) -> str | None:
    m = _MARKER_RE.search(body or "")
    return m.group(1) if m else None


def marker_resolved_at(body: str | None) -> str | None:
    m = _MARKER_RE.search(body or "")
    return m.group(2) if m else None


def check_meta(
    pr: str, meta: dict[str, Any], repo: str, eval_sha: str
) -> tuple[bool, str]:
    """Condition (a)."""
    head = meta.get("head") or {}
    if ((meta.get("user") or {}).get("login")) != RESYNC_AUTHOR:
        return False, f"PR #{pr}: author is not {RESYNC_AUTHOR} — skipping."
    if head.get("ref") != RESYNC_BRANCH:
        return False, f"PR #{pr}: head branch is not {RESYNC_BRANCH} — skipping."
    if ((head.get("repo") or {}).get("full_name")) != repo:
        return False, f"PR #{pr}: head is not in {repo} (fork?) — skipping."
    if ((meta.get("base") or {}).get("ref")) != BASE_BRANCH:
        return False, f"PR #{pr}: base is not {BASE_BRANCH} — skipping."
    if meta.get("state") != "open" or meta.get("draft"):
        return False, f"PR #{pr}: not open, or a draft — skipping."
    if not eval_sha or head.get("sha") != eval_sha:
        return False, f"PR #{pr}: HEAD moved since this run was triggered — skipping."
    return True, ""


def check_commits(pr: str, commits: list[Any], head_sha: str) -> tuple[bool, str, str]:
    """Condition (c): ``(ok, message, parent_sha)``."""
    if len(commits) != 1:
        return (
            False,
            f"PR #{pr}: {len(commits)} commits (expected the lane's one) — skipping.",
            "",
        )
    c = commits[0] if isinstance(commits[0], dict) else {}
    if c.get("sha") != head_sha:
        return False, f"PR #{pr}: the commit is not the PR head — skipping.", ""
    if ((c.get("author") or {}).get("login")) != RESYNC_AUTHOR:
        return (
            False,
            f"PR #{pr}: commit not authored by {RESYNC_AUTHOR} — skipping.",
            "",
        )
    parents = c.get("parents") or []
    if len(parents) != 1 or not (parents[0] or {}).get("sha"):
        return (
            False,
            f"PR #{pr}: commit does not have exactly one parent — skipping.",
            "",
        )
    return True, "", str(parents[0]["sha"])


def pinned_conformance(uv_lock_text: str) -> str | None:
    """The conformance version a ``uv.lock`` resolves, or None."""
    try:
        data = tomllib.loads(uv_lock_text)
    except tomllib.TOMLDecodeError:
        return None
    for pkg in data.get("package", []):
        if isinstance(pkg, dict) and pkg.get("name") == CONFORMANCE_PACKAGE:
            version = pkg.get("version")
            if isinstance(version, str) and _SEMVER_RE.match(version):
                return version
            return None
    return None


def parse_manifest(stdout: str) -> dict | None:
    """The last ``--json`` manifest line (an object with a ``touched`` list)."""
    manifest = None
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            doc = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("touched"), list):
            manifest = doc
    return manifest


def _normalise(line: str) -> str:
    # A reordered JSON object flips `"x": "y",` <-> `"x": "y"` on its last
    # member; strip that so a pure reorder never reads as a lost setting.
    return line.strip().rstrip(",").strip()


_YAML_SUFFIXES = (".yaml", ".yml")
_YAML_KEY_VALUE = re.compile(r"^((?:-\s+)?[\w.-]+:)\s+(\S.*)$")
_YAML_INDICATORS = frozenset("-?:,[]{}#&*!|>'\"%@`")
_YAML_KEYWORDS = frozenset(
    {"~", "null", "true", "false", "yes", "no", "on", "off", "y", "n"}
)
_YAML_NUMBER_LIKE = re.compile(r"^[+.]?\d|^[+]?\.(inf|nan)$", re.IGNORECASE)
_YAML_BLOCK_SCALAR = re.compile(r"^[|>][+-]?\d?[+-]?\s*(#.*)?$")


def _unquote_yaml_scalar(value: str) -> str:
    """``value`` without its quotes when YAML reads it the same either way."""
    if len(value) < 3 or value[0] != value[-1] or value[0] not in "\"'":
        return value
    inner = value[1:-1]
    if (
        inner[0] in _YAML_INDICATORS
        or inner != inner.strip()
        or any(c in inner for c in ":#\\\"'")
        or inner.lower() in _YAML_KEYWORDS
        or _YAML_NUMBER_LIKE.search(inner)
    ):
        return value
    return inner


def _normalise_for(path: str, line: str) -> str:
    norm = _normalise(line)
    if not path.endswith(_YAML_SUFFIXES):
        return norm
    match = _YAML_KEY_VALUE.match(norm)
    if not match:
        return norm
    return f"{match.group(1)} {_unquote_yaml_scalar(match.group(2))}"


def _description_lines(path: str, text: str) -> set[int]:
    """Indexes of ``description`` lines in a YAML or JSON file, including the
    body of a YAML block-scalar description. Descriptions are template prose,
    so a reworded one is not a lost setting."""
    if not path.endswith((*_YAML_SUFFIXES, ".json")):
        return set()
    skip: set[int] = set()
    block_indent: int | None = None
    for i, line in enumerate(text.splitlines()):
        indent = len(line) - len(line.lstrip())
        if block_indent is not None and (not line.strip() or indent > block_indent):
            skip.add(i)
            continue
        block_indent = None
        if _setting_key(line) != "description":
            continue
        skip.add(i)
        value = line.split(":", 1)[1].strip()
        if path.endswith(_YAML_SUFFIXES) and _YAML_BLOCK_SCALAR.match(value):
            stripped = line.lstrip()
            block_indent = indent + len(stripped) - len(stripped.lstrip("- "))
    return skip


# Settings the canonical templates deliberately stopped carrying: a render
# that drops one of these is the intended change, not a lost per-repo value.
# Keyed by repo-relative path; matched on the YAML/JSON key. The lane imports
# this mapping, so there is one copy.
ACCEPTED_DROPS: dict[str, frozenset[str]] = {
    ".github/workflows/tests.yaml": frozenset(
        {"container-health-timeout-seconds", "e2e-clouds"}
    ),
}


def _setting_key(line: str) -> str:
    head = line.strip().split(":", 1)[0] if ":" in line else ""
    return head.strip().strip('"').strip("'").lstrip("-").strip()


def still_lost(path: str, lost: list[str]) -> list[str]:
    """Lost lines for ``path`` minus the accepted drops.

    Each accepted key excuses ONE lost line: the canonical template carried
    that setting once. A second lost line with the same key is a repo's own
    value that happens to share the name, and it is still reported.
    """
    accepted = ACCEPTED_DROPS.get(path, frozenset())
    excused: set[str] = set()
    remaining: list[str] = []
    for line in lost:
        key = _setting_key(line)
        if key in accepted and key not in excused:
            excused.add(key)
            continue
        remaining.append(line)
    return remaining


def lost_setting_lines(backup_text: str, new_text: str, path: str = "") -> list[str]:
    """Non-comment lines in the ``.bak`` absent from its replacement
    (reorder-immune, counted per line so a duplicate elsewhere in the file
    cannot stand in for a removed one). In a YAML or JSON ``path``,
    descriptions are not compared, and in YAML a value that only gained or
    lost its quotes still matches. The lane imports this function."""
    remaining = Counter(_normalise_for(path, x) for x in new_text.splitlines())
    skip = _description_lines(path, backup_text)
    lost: list[str] = []
    for i, line in enumerate(backup_text.splitlines()):
        if i in skip:
            continue
        norm = _normalise_for(path, line)
        if not norm or norm.startswith("#") or norm.startswith("//"):
            continue
        if norm in {"{", "}", "[", "]", "},", "],"}:
            continue
        if remaining[norm] > 0:
            remaining[norm] -= 1
        elif line.strip() not in lost:
            lost.append(line.strip())
    return lost


def safe_touched(manifest: dict, root: pathlib.Path) -> list[str]:
    """The lane's staging filter: manifest paths only, no escapes, no backups,
    nothing reached through a symlinked directory."""
    out: set[str] = set()
    for p in manifest.get("touched") or []:
        if not isinstance(p, str) or not p:
            continue
        if p.startswith("/") or ".." in p.split("/") or p.endswith(".bak"):
            continue
        cur = root
        via_link = False
        for part in pathlib.PurePosixPath(p).parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                via_link = True
                break
        if not via_link:
            out.add(p)
    return sorted(out)


def symlink_skipped(manifest: dict, root: pathlib.Path) -> list[str]:
    """Manifest paths under a directory ``root`` has as a symlink: the render
    touched them, but :func:`copy_back` and :func:`safe_touched` skip them, so
    the repo never receives them. The lane reports them."""
    return sorted(
        {
            p
            for p in manifest.get("touched") or []
            if isinstance(p, str) and _via_symlink(root, p)
        }
    )


def count_resync_approvals(reviews: list[Any], head_sha: str) -> int:
    """Condition (h): atlan-ci approvals of THIS head with the resync signature."""
    return sum(
        1
        for r in reviews
        if isinstance(r, dict)
        and ((r.get("user") or {}).get("login")) == APPROVER_LOGIN
        and r.get("state") == "APPROVED"
        and r.get("commit_id") == head_sha
        and str(r.get("body") or "").startswith(RESYNC_SIGNATURE)
    )


# ---------------------------------------------------------------------------
# Re-render (the proof)
# ---------------------------------------------------------------------------


@dataclass
class RenderResult:
    tree_matches: bool
    suite: str | None = None
    lost: dict[str, list[str]] = field(default_factory=dict)
    note: str = ""


def git_env() -> dict[str, str]:
    """Token in env only (GIT_CONFIG_*) — never on argv or in a remote URL."""
    token = os.environ.get("GH_TOKEN", "")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(args: list[str], cwd: str, runner: Runner) -> str:
    result = call(
        runner,
        ["git", *args],
        timeout=GIT_TIMEOUT,
        cwd=cwd,
        env=git_env(),
    )
    if result.returncode != 0:
        raise GhError(f"git {args[0]} failed: {(result.stderr or '')[-300:]}")
    return result.stdout or ""


def stage_like_the_lane(
    work: str, manifest: dict, runner: Runner
) -> dict[str, list[str]]:
    """Apply the lane's ``.bak`` discipline and staging to the render in
    ``work``; return lost settings. The lane imports this function."""
    root = pathlib.Path(work)
    lost: dict[str, list[str]] = {}
    backups = sorted(
        p for p in root.rglob("*.bak") if ".git" not in p.relative_to(root).parts
    )
    for bak in backups:
        original = bak.with_suffix("")
        if original.exists():
            rel = str(original.relative_to(root))
            missing = still_lost(
                rel,
                lost_setting_lines(
                    bak.read_text(encoding="utf-8", errors="replace"),
                    original.read_text(encoding="utf-8", errors="replace"),
                    rel,
                ),
            )
            if missing:
                lost[rel] = missing
        bak.unlink()
    touched = safe_touched(manifest, root)
    if touched:
        _git(["add", "-A", "-f", "--", *touched], work, runner)
    return lost


def parent_automerge_mode(repo: str, parent_sha: str, runner: Runner) -> str:
    """Condition (e0): the repo's renovate.json at the render base, classified
    by the fleet's shared rule. ``auto`` is the only approvable answer; soft,
    unknown, missing and unreadable all withhold the approval.

    Only a 404 means missing. Any other failure (a rate limit, a transport
    error) raises :class:`GhError`, so a transient API problem shows as a red
    step rather than a quiet skip that reads as an absent file."""
    result = call(
        runner,
        [
            "gh",
            "api",
            f"repos/{repo}/contents/renovate.json?ref={parent_sha}",
            "-q",
            ".content",
        ],
    )
    if result.returncode != 0:
        if _is_not_found(result):
            return "missing"
        raise GhError(
            f"reading renovate.json at {parent_sha[:12]}: "
            f"{(result.stderr or '').strip()[-300:]}"
        )
    if not (result.stdout or "").strip():
        return "missing"
    try:
        text = base64.b64decode(result.stdout).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return "unreadable"
    return discover.automerge_mode(text)


def parent_pinned_conformance(repo: str, parent_sha: str, runner: Runner) -> str | None:
    """The conformance version ``uv.lock`` resolves at the render base, read
    raw (``uv.lock`` routinely exceeds the contents API's 1 MB base64 cap).
    ``None`` only for a 404 or an empty file; any other failure raises."""
    result = call(
        runner,
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{repo}/contents/uv.lock?ref={parent_sha}",
        ],
    )
    if result.returncode != 0:
        if _is_not_found(result):
            return None
        raise GhError(
            f"reading uv.lock at {parent_sha[:12]}: "
            f"{(result.stderr or '').strip()[-300:]}"
        )
    if not (result.stdout or "").strip():
        return None
    return pinned_conformance(result.stdout)


def parent_preconditions(
    repo: str, parent_sha: str, suite: str, runner: Runner
) -> tuple[bool, str]:
    """The gate's checks on the render base that do not need a render: its
    ``uv.lock`` pins ``suite`` (condition e, first half) and its
    ``renovate.json`` is in auto-merge mode (condition e0).

    The lane calls this too before leaving an unchanged PR alone. A PR whose
    parent fails it can never be approved however often the approver is
    dispatched, so the lane must re-push it onto current main instead.
    """
    pinned = parent_pinned_conformance(repo, parent_sha, runner)
    if pinned != suite:
        return False, f"uv.lock at the parent pins {pinned}, the PR marker says {suite}"
    mode = parent_automerge_mode(repo, parent_sha, runner)
    if mode != "auto":
        return False, f"renovate.json at the parent is {mode}, not auto-merge"
    return True, ""


def render_and_compare(
    repo: str,
    parent_sha: str,
    head_sha: str,
    suite: str,
    resolved_at: str,
    runner: Runner,
) -> RenderResult:
    """Condition (e)/(f): render the parent at ``suite`` and compare trees."""
    with tempfile.TemporaryDirectory(prefix="resync-verify-") as tmp:
        work = os.path.join(tmp, "repo")
        os.makedirs(work)
        _git(["init", "-q"], work, runner)
        _git(["remote", "add", "origin", f"https://github.com/{repo}"], work, runner)
        _git(
            ["fetch", "-q", "--depth", "1", "origin", parent_sha, head_sha],
            work,
            runner,
        )
        _git(["checkout", "-q", "--detach", parent_sha], work, runner)
        lock = pathlib.Path(work, "uv.lock")
        pinned = (
            pinned_conformance(lock.read_text(encoding="utf-8"))
            if lock.is_file()
            else None
        )
        if pinned != suite:
            return RenderResult(
                False,
                pinned,
                note=f"base uv.lock pins {pinned}, PR marker says {suite}",
            )
        rc, stdout, _ = sandboxed_render(work, suite, resolved_at, runner)
        manifest = parse_manifest(stdout)
        if rc != 0 or manifest is None or manifest.get("skipped"):
            return RenderResult(
                False, suite, note=f"bootstrap render failed (exit {rc})"
            )
        lost = stage_like_the_lane(work, manifest, runner)
        rendered_tree = _git(["write-tree"], work, runner).strip()
        head_tree = _git(["rev-parse", f"{head_sha}^{{tree}}"], work, runner).strip()
        matches = bool(rendered_tree) and rendered_tree == head_tree
        return RenderResult(
            matches,
            suite,
            lost,
            note="" if matches else "rendered tree differs from the PR head",
        )


# ---------------------------------------------------------------------------
# GitHub I/O + orchestration (shared with the lane, conformance_resync.py)
# ---------------------------------------------------------------------------


def gh_json(args: list[str], runner: Runner, *, what: str) -> Any:
    result = call(runner, ["gh", *args])
    if result.returncode != 0:
        raise GhError(f"{what}: {(result.stderr or '').strip()[-300:]}")
    try:
        return json.loads(result.stdout or "null")
    except ValueError as exc:
        raise GhError(f"{what}: unparseable response") from exc


def flatten(payload: Any) -> list[Any]:
    """``--paginate --slurp`` yields a list of pages; flatten to one list."""
    if (
        isinstance(payload, list)
        and payload
        and all(isinstance(p, list) for p in payload)
    ):
        return [item for page in payload for item in page]
    return payload if isinstance(payload, list) else []


def required_checks_green(
    repo: str, pr: str, runner: Runner, *, echo: bool = True
) -> bool:
    """``gh pr checks --required`` exits 0 iff every required check is green.
    The lane calls this too, with ``echo=False`` to keep its log to one line
    per repo."""
    checks = call(runner, ["gh", "pr", "checks", pr, "--repo", repo, "--required"])
    if echo:
        for stream in (checks.stdout, checks.stderr):
            if stream and stream.strip():
                print(stream.rstrip())
    return checks.returncode == 0


def commit_time(commit: Any) -> datetime | None:
    """The committer date of a ``pulls/{n}/commits`` entry."""
    date = (((commit or {}).get("commit") or {}).get("committer") or {}).get("date")
    if not isinstance(date, str):
        return None
    try:
        return datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        return None


def process_resync_pr(
    repo: str,
    pr: str,
    eval_sha: str,
    meta: dict[str, Any],
    runner: Runner,
    renderer: Callable[..., RenderResult] = render_and_compare,
) -> bool:
    """Evaluate one resync PR; approve iff every condition holds. Returns True
    iff a new approval was posted."""
    ok, message = check_meta(pr, meta, repo, eval_sha)
    if not ok:
        print(message)
        return False
    head_sha = str((meta.get("head") or {}).get("sha"))
    suite = marker_suite_version(meta.get("body"))
    resolved_at = marker_resolved_at(meta.get("body"))
    if not suite or not resolved_at:
        print(f"PR #{pr}: no conformance-resync marker in the body — skipping.")
        return False

    commits = flatten(
        gh_json(
            ["api", f"repos/{repo}/pulls/{pr}/commits", "--paginate", "--slurp"],
            runner,
            what=f"listing commits for PR #{pr}",
        )
    )
    ok, message, parent_sha = check_commits(pr, commits, head_sha)
    if not ok:
        print(message)
        return False

    # The lane picks resolved-at before it commits, so it is never later than
    # the commit or than now. A body edited to a later date is refused.
    committed = commit_time(commits[0])
    if committed is None:
        print(f"PR #{pr}: the lane commit has no committer date — skipping.")
        return False
    ok, why = check_resolved_at(resolved_at, committed, datetime.now(UTC))
    if not ok:
        print(f"PR #{pr}: {why} — skipping.")
        return False

    base_ref = str((meta.get("base") or {}).get("ref") or "")
    compare = gh_json(
        ["api", f"repos/{repo}/compare/{parent_sha}...{base_ref}"],
        runner,
        what=f"checking PR #{pr}'s base ancestry",
    )
    if not isinstance(compare, dict) or compare.get("status") not in {
        "identical",
        "ahead",
    }:
        print(f"PR #{pr}: its parent commit is not in {base_ref}'s history — skipping.")
        return False

    ok, why = parent_preconditions(repo, parent_sha, suite, runner)
    if not ok:
        print(f"PR #{pr}: {why} — skipping.")
        return False

    # Cheapest first: the render pulls an image and installs packages, so it
    # runs only once nothing cheaper can refuse the PR.
    if not required_checks_green(repo, pr, runner):
        print(f"PR #{pr}: required checks not yet all green — skipping.")
        return False

    reviews = flatten(
        gh_json(
            ["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate", "--slurp"],
            runner,
            what=f"listing reviews for PR #{pr}",
        )
    )
    if count_resync_approvals(reviews, head_sha):
        print(
            f"PR #{pr}: already approved at this head with the resync signature — skipping."
        )
        return False

    print(
        f"PR #{pr}: re-rendering bootstrap --resync at conformance {suite} "
        f"on {parent_sha[:12]}..."
    )
    result = renderer(repo, parent_sha, head_sha, suite, resolved_at, runner)
    if result.lost:
        print(
            f"PR #{pr}: the render drops per-repo settings in "
            f"{sorted(result.lost)} — skipping."
        )
        return False
    if not result.tree_matches:
        print(f"PR #{pr}: {result.note or 'render does not match the PR'} — skipping.")
        return False
    print(f"PR #{pr}: PR tree is byte-identical to the independent render.")

    # Re-run condition (a) on the live PR, not just its head: a same-repo
    # writer can retarget the base, convert it to a draft or close it while
    # the render runs, all without moving the head SHA.
    live = gh_json(
        ["api", f"repos/{repo}/pulls/{pr}"],
        runner,
        what=f"re-reading PR #{pr}",
    )
    ok, message = check_meta(pr, live if isinstance(live, dict) else {}, repo, head_sha)
    if not ok:
        print(f"{message} (changed during verification)")
        return False
    call(
        runner,
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr}/reviews",
            "-X",
            "POST",
            "-f",
            f"commit_id={head_sha}",
            "-f",
            "event=APPROVE",
            "-f",
            f"body={RESYNC_APPROVAL_BODY}",
        ],
        check=True,
    )
    print(f"✅ Approved PR #{pr} as atlan-ci (conformance resync auto-approval).")
    return True
