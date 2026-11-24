"""Which application-sdk the app under review pins, as a git ref (FND-5331).

The reviewer can read the Atlan Application SDK source at the exact version the
app pins, so it can check how an SDK API the diff calls behaves instead of
judging it from memory. This module answers one question from the app's
`uv.lock`: which ref of `atlanhq/application-sdk` to check out.

The lock is the pull request HEAD's (FND-5316), read through the REST API as
data (`head_lock`), never checked out: the pull request is what runs after the
merge, and an SDK bump in it changes which SDK its code calls. The default
branch's lock is not used: on a pull request that pins another SDK it named
the wrong version. The ref is therefore chosen by the pull request's author,
and the checks below are what bound it: a release tag or a locked commit of
atlanhq/application-sdk, from PyPI or its mirror, read as text and never run.

- A registry pin from PyPI, or the org's registry firewall that mirrors it
  (`_KNOWN_REGISTRIES`; any other index falls back), of version
  `X.Y.Z` or `X.Y.ZrcN` is the release tag `vX.Y.Z` / `vX.Y.ZrcN`, the tag scheme
  application-sdk's releases use.
- A git pin of `github.com/atlanhq/application-sdk` is the locked commit: uv
  writes it after `#` in the source URL, whatever `rev=`/`branch=`/`tag=` says.
- `atlan-application-sdk-api`, the subset of `application_sdk/` the
  consolidated API host installs (packages/api), ships from this repository
  too, in lockstep: its version N is the tag `vN`. It is the pin only when the
  lock has no `atlan-application-sdk`.
- Anything else is no ref: the package absent, more than one distinct pin, an
  editable or path source, a git pin of another repository (its commit is not in
  atlanhq/application-sdk), a version with no release tag, or a lock that does
  not parse. The review then runs without the SDK source and says so.

An app can also pin an Atlan SDK package that does not ship from this
repository (`atlan-server-sdk`, from its own repository, which the workflow's
token cannot read). `sdk:` cannot cover it, so `other_pins` finds such pins in
the head's lock and `pyproject.toml` files and `pins_block` tells the line
review which package and version the app runs instead.

The workflow calls `python -m connector_reviewer sdk-ref --repo … --event-name
… --event-path …`, which writes the ref as a step output for the checkout step
(`step_output`): the decision is here, tested, and not in workflow shell
(docs/standards/ci.md). The tools that read the checkout are `tools.SdkSource`.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from .typed import EMPTY

if TYPE_CHECKING:
    from .github import GitHub

PACKAGE = "atlan-application-sdk"
# Ships from this repository at the same tag as the SDK (docs/standards/api-distribution.md).
API_PACKAGE = "atlan-application-sdk-api"
# The pin is the first of these the lock has.
IN_REPO_PACKAGES = (PACKAGE, API_PACKAGE)
# Ships from this repository too, but it is a CI tool, not code the app calls.
_TOOL_PACKAGES = ("atlan-application-sdk-conformance",)
REPOSITORY = "atlanhq/application-sdk"
# The SDK checkout holds this directory only (a sparse checkout).
PACKAGE_DIR = "application_sdk"
LOCK = "uv.lock"

# Exactly the versions application-sdk tags as `v<version>`. Anything looser (a
# post or dev release, a local version) has no tag, and a checkout of a missing
# ref fails, so it is refused here instead.
_RELEASE = re.compile(r"\d+\.\d+\.\d+(?:rc\d+)?")
_COMMIT = re.compile(r"[0-9a-f]{40}")
# The two ways a ref reaches the workflow: what `resolve` returns, and nothing
# else is written as a step output.
_REF = re.compile(rf"v{_RELEASE.pattern}|{_COMMIT.pattern}")
# A git pin's version is only shown, never checked out, but it is written to
# `$GITHUB_OUTPUT` and the prompt: a TOML string can hold a newline or a quote,
# so anything outside these characters is left out of the label.
_VERSION = re.compile(r"[0-9A-Za-z.+!_-]{1,64}")
_LABEL = re.compile(r"[0-9A-Za-z.+!_ -]{1,100}")


@dataclass(frozen=True)
class SdkPin:
    """The application-sdk the app pins. `ref` is safe to check out: it matches
    `_REF` by construction."""

    ref: str  # "v3.40.0" or a 40-hex commit
    version: str  # the version uv.lock records, e.g. "3.40.0"; "" when unsafe to show
    kind: str  # "release" | "git"
    package: str = PACKAGE  # one of IN_REPO_PACKAGES

    @property
    def label(self) -> str:
        """How the prompt and the log name it."""
        if self.kind == "git":
            at = f"commit {self.ref[:12]}"
            return f"{self.version} at {at}" if self.version else at
        return self.ref


@dataclass(frozen=True)
class Resolution:
    pin: SdkPin | None
    reason: str = ""  # why there is no pin; empty when there is one


def _lock_entries(lock_text: str) -> list[dict[str, object]] | str:
    """The `[[package]]` tables of a `uv.lock`, or why it does not parse."""
    try:
        lock = tomllib.loads(lock_text)
    except (tomllib.TOMLDecodeError, TypeError, ValueError) as e:
        return f"uv.lock does not parse ({type(e).__name__})"
    packages = lock.get("package")
    if not isinstance(packages, list):
        return []
    return [
        cast(dict[str, object], p)
        for p in cast(list[object], packages)
        if isinstance(p, dict)
    ]


def resolve(lock_text: str) -> Resolution:
    """The pinned application-sdk in a `uv.lock`'s text. Never raises."""
    all_entries = _lock_entries(lock_text)
    if isinstance(all_entries, str):
        return Resolution(None, all_entries)
    package, entries = PACKAGE, []
    for package in IN_REPO_PACKAGES:
        entries = [e for e in all_entries if e.get("name") == package]
        if entries:
            break
    if not entries:
        return Resolution(None, f"{PACKAGE} is not in uv.lock")
    pins: set[tuple[str, str, str]] = set()
    for entry in entries:
        one = _pin_of(entry, package)
        if isinstance(one, str):
            return Resolution(None, one)
        pins.add((one.ref, one.version, one.kind))
    if len(pins) > 1:
        return Resolution(None, f"uv.lock pins {package} more than one way")
    ref, version, kind = pins.pop()
    return Resolution(SdkPin(ref=ref, version=version, kind=kind, package=package))


def _pin_of(entry: dict[str, object], package: str = PACKAGE) -> SdkPin | str:
    """One `[[package]]` entry as a pin, or the reason it is not one."""
    version = entry.get("version")
    raw_source = entry.get("source")
    if not isinstance(version, str) or not isinstance(raw_source, dict):
        return f"{package} in uv.lock has no version or source"
    source = cast(dict[str, object], raw_source)
    if "registry" in source:
        if not _known_registry(source.get("registry")):
            # Another index could serve different contents under the same
            # version, so the release tag would not be what the app runs.
            return f"{package} comes from a registry that is not PyPI or its mirror"
        if not _RELEASE.fullmatch(version):
            return f"{package} {version!r} is not a released version with a tag"
        return SdkPin(ref=f"v{version}", version=version, kind="release")
    git = source.get("git")
    if isinstance(git, str):
        try:
            url = urllib.parse.urlsplit(git)
            host = url.hostname
        except ValueError:
            return f"{package}'s git source in uv.lock is malformed"
        repo = url.path.strip("/").removesuffix(".git")
        if url.scheme != "https" or host != "github.com" or repo != REPOSITORY:
            return f"{package} is pinned to a git source other than {REPOSITORY}"
        commit = url.fragment
        if not _COMMIT.fullmatch(commit):
            return f"{package}'s git pin in uv.lock has no locked commit"
        shown = version if _VERSION.fullmatch(version) else ""
        return SdkPin(ref=commit, version=shown, kind="git")
    kinds = ", ".join(sorted(source)) or "empty"
    return f"{package} comes from a {kinds} source, not a release or a git commit"


# Registries whose `atlan-application-sdk` is the PyPI release, so a version
# maps to application-sdk's own release tag: PyPI, and the org's registry
# firewall, which mirrors it. Exact host and path prefix; anything else falls back.
_KNOWN_REGISTRIES = (
    ("pypi.org", "/simple"),
    ("factory.endorlabs.com", "/v1/namespaces/atlan.atlanhq/firewall/pypi/simple"),
)


def _known_registry(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        url = urllib.parse.urlsplit(raw)
        host = url.hostname
    except ValueError:
        return False
    path = url.path.rstrip("/")
    return url.scheme == "https" and any(
        host == h and (path == p or path.startswith(p + "/"))
        for h, p in _KNOWN_REGISTRIES
    )


def resolve_root(root: Path) -> Resolution:
    """The pin in `<root>/uv.lock`, the app checkout's lock."""
    lock = root / "uv.lock"
    try:
        if lock.is_symlink() or not lock.is_file():
            return Resolution(None, "the app has no uv.lock")
        text = lock.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return Resolution(None, f"uv.lock could not be read ({type(e).__name__})")
    return resolve(text)


@dataclass(frozen=True)
class HeadLock:
    """The pull request head's `uv.lock` text, or why there is none."""

    text: str | None
    reason: str = ""  # why `text` is None; empty when there is a lock
    head: str = ""  # the head commit it was read at (40 hex), "" when none

    def resolution(self) -> Resolution:
        if self.text is None:
            return Resolution(None, self.reason)
        return resolve(self.text)


def head_lock(gh: GitHub, number: int) -> HeadLock:
    """The `uv.lock` at pull request `number`'s current head, read through the
    REST API as data. What `sdk-ref` uses; the review reads at the head it
    reviews instead (`head_lock_at`). Never raises."""
    if number <= 0:
        return HeadLock(None, "no pull request to read uv.lock from")
    try:
        head = str(((gh.pr(number) or EMPTY).get("head") or EMPTY).get("sha") or "")
    except Exception as e:  # noqa: BLE001 - a failed read is a reason, never a crash
        return HeadLock(
            None, f"the pull request could not be read ({type(e).__name__})"
        )
    return head_lock_at(gh, head)


def head_lock_at(gh: GitHub, head: str) -> HeadLock:
    """The `uv.lock` at commit `head` (`GitHub.raw_file_at`: an app's lock can
    pass the 1 MB the JSON contents reply carries). Never raises."""
    if not _COMMIT.fullmatch(head):
        return HeadLock(None, "the pull request has no head commit")
    try:
        text = gh.raw_file_at(LOCK, head)
    except Exception:  # noqa: BLE001 - a network failure is a reason, never a crash
        text = None
    if text is None:
        return HeadLock(
            None,
            f"the pull request head ({head[:9]}) has no uv.lock, or it could not be read",
            head,
        )
    return HeadLock(text, head=head)


# ---- Atlan SDK packages that `sdk:` cannot cover (FND-5316) ---------------------

# An Atlan SDK package by name: `atlan-…sdk…` (PEP 503 normalised). The ones that
# ship from this repository are excluded: `sdk:` covers them, or they are tools.
_ATLAN_SDK = re.compile(r"atlan-[a-z0-9-]*sdk[a-z0-9-]*")
# What a PEP 508 requirement starts with: a name, then optional extras.
_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)")
# A `name @ git+https://host/owner/repo(.git)@rev` direct reference.
_GIT_REF = re.compile(
    r"@\s*git\+(https://[^\s@#;]+?)(?:\.git)?(?:@([^\s#;]+))?(?:[#;\s]|$)"
)
# What may be shown of a version or specifier: the prompt is no place for
# arbitrary pull-request text.
_SHOWN = re.compile(r"[0-9A-Za-z.,<>=!~*+ _-]{1,60}")
_SHOWN_REPO = re.compile(r"[0-9A-Za-z._-]{1,100}/[0-9A-Za-z._-]{1,100}")
# Bounds on what `other_pins` reads.
MAX_PYPROJECTS = 20
MAX_PYPROJECT_CHARS = 200_000


@dataclass(frozen=True)
class OtherPin:
    """An Atlan SDK package the app pins that does not ship from
    atlanhq/application-sdk, so `sdk:` cannot show its source."""

    package: str  # normalised name, e.g. "atlan-server-sdk"
    pinned: str  # how it is pinned, e.g. "git github.com/atlanhq/server-sdk at commit 761bbe535963"
    where: str  # the file that pins it: "uv.lock" or a pyproject.toml path


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _is_other_sdk(name: str) -> bool:
    n = _normalise(name)
    return (
        _ATLAN_SDK.fullmatch(n) is not None
        and n not in IN_REPO_PACKAGES
        and n not in _TOOL_PACKAGES
    )


def _git_label(url: str, rev: str) -> str:
    """`git github.com/owner/repo at commit abc123def456`, from parts that are
    each shown only when they are plain."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return "git (source not shown)"
    repo = parts.path.strip("/").removesuffix(".git")
    where = (
        f"{host}/{repo}"
        if host == "github.com" and _SHOWN_REPO.fullmatch(repo)
        else "a git source"
    )
    if _COMMIT.fullmatch(rev):
        return f"git {where} at commit {rev[:12]}"
    if rev and _VERSION.fullmatch(rev):
        return f"git {where} at {rev}"
    return f"git {where}"


def _lock_pins(lock_text: str) -> list[OtherPin]:
    entries = _lock_entries(lock_text)
    if isinstance(entries, str):
        return []
    out: list[OtherPin] = []
    for e in entries:
        name = e.get("name")
        if not isinstance(name, str) or not _is_other_sdk(name):
            continue
        version = e.get("version")
        shown = (
            version if isinstance(version, str) and _VERSION.fullmatch(version) else ""
        )
        raw = e.get("source")
        source = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        git = source.get("git")
        if isinstance(git, str):
            url, _, commit = git.partition("#")
            pinned = _git_label(url.split("?", 1)[0], commit)
            pinned = f"{shown} from {pinned}" if shown else pinned
        else:
            pinned = shown or "version not shown"
        out.append(OtherPin(_normalise(name), pinned, LOCK))
    return out


def _requirements(doc: dict[str, object]) -> list[str]:
    """Every PEP 508 string of a pyproject: dependencies, optional
    dependencies and dependency groups."""
    out: list[object] = []
    project = doc.get("project")
    if isinstance(project, dict):
        p = cast(dict[str, object], project)
        deps = p.get("dependencies")
        if isinstance(deps, list):
            out.extend(cast(list[object], deps))
        optional = p.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in cast(dict[str, object], optional).values():
                if isinstance(group, list):
                    out.extend(cast(list[object], group))
    groups = doc.get("dependency-groups")
    if isinstance(groups, dict):
        for group in cast(dict[str, object], groups).values():
            if isinstance(group, list):
                out.extend(cast(list[object], group))
    return [r for r in out if isinstance(r, str)]


def _pyproject_pins(path: str, text: str) -> list[OtherPin]:
    try:
        doc = tomllib.loads(text[:MAX_PYPROJECT_CHARS])
    except (tomllib.TOMLDecodeError, TypeError, ValueError):
        return []
    out: list[OtherPin] = []
    for req in _requirements(doc):
        m = _REQUIREMENT.match(req)
        if not m or not _is_other_sdk(m.group(1)):
            continue
        rest = m.group(3).split(";", 1)[0].strip()
        git = _GIT_REF.search(rest)
        if git:
            pinned = _git_label(git.group(1), git.group(2) or "")
        elif rest and _SHOWN.fullmatch(rest):
            pinned = rest
        else:
            pinned = "no version" if not rest else "version not shown"
        out.append(OtherPin(_normalise(m.group(1)), pinned, path))
    return out


def other_pins(lock_text: str | None, pyprojects: dict[str, str]) -> list[OtherPin]:
    """The Atlan SDK packages the app pins that `sdk:` cannot cover: from the
    lock, then from each `pyproject.toml` (a package outside the uv workspace,
    such as a `server/` package with its own direct reference, is in no lock).
    One entry per package and pin; at most `MAX_PYPROJECTS` files are read."""
    found: list[OtherPin] = _lock_pins(lock_text) if lock_text else []
    for path in sorted(pyprojects)[:MAX_PYPROJECTS]:
        found.extend(_pyproject_pins(path, pyprojects[path]))
    out: list[OtherPin] = []
    seen: set[tuple[str, str]] = set()
    for p in found:
        if (p.package, p.pinned) not in seen:
            seen.add((p.package, p.pinned))
            out.append(p)
    return out


def is_pyproject(path: str) -> bool:
    return PurePosixPath(path).name == "pyproject.toml"


def pins_block(others: list[OtherPin], sdk_label: str | None) -> str:
    """What the line review is told when the app pins an Atlan SDK package
    `sdk:` does not cover; "" when it pins none. `sdk_label` is the
    application-sdk version `sdk:` reads, or None without the SDK source."""
    if not others:
        return ""
    covered = (
        f"atlan-application-sdk {sdk_label}"
        if sdk_label
        else "atlan-application-sdk, and its source is not available in this review"
    )
    lines = [
        f"- {html.escape(p.package)}: {html.escape(p.pinned)} (in {html.escape(_shown_path(p.where))})"
        for p in others
    ]
    return (
        "<other_sdk_packages>\n"
        f"`sdk:` covers only application-sdk ({html.escape(covered)}). This app also pins Atlan "
        "SDK packages that do not ship from application-sdk, so their source is not readable here:\n"
        + "\n".join(lines)
        + "\nCode that imports one of these packages calls that package, not application-sdk. Do not "
        "judge its calls (signatures, return types, what exists) from application-sdk source or from "
        "memory of it. When a finding depends on how such a call behaves, say which package and "
        "version it rests on and that its source was not read.\n"
        "</other_sdk_packages>"
    )


def _shown_path(path: str) -> str:
    return path if re.fullmatch(r"[0-9A-Za-z._/-]{1,200}", path) else "a pyproject.toml"


def step_output(res: Resolution, head: str = "") -> str:
    """The `$GITHUB_OUTPUT` lines for the checkout step: `ref` empty unless it is
    one of the two safe shapes, so the step's `if:` skips the checkout. `head`,
    the commit the lock was read at, is written only when it is 40 hex: the
    review names it when the pull request has moved since."""
    pin = res.pin
    ok = pin is not None and _REF.fullmatch(pin.ref) is not None
    ref = pin.ref if pin is not None and ok else ""
    label = pin.label if pin is not None and ok else ""
    if not _LABEL.fullmatch(label):
        label = ""
    out = f"ref={ref}\nlabel={label}\n"
    return out + (f"head={head}\n" if _COMMIT.fullmatch(head) else "")


def checkout_present(path: Path) -> bool:
    """True when `path` holds an application-sdk checkout with Python in it: an
    absent directory, a skipped step and a failed checkout (a `.git` and
    nothing else) are all false."""
    pkg = path / PACKAGE_DIR
    try:
        return (
            pkg.is_dir()
            and not pkg.is_symlink()
            and (pkg / "__init__.py").is_file()
            and not (pkg / "__init__.py").is_symlink()
        )
    except OSError:
        return False


GIT_SECONDS = 10.0  # each `git rev-parse` of `checkout_commit`


def checkout_commit(path: Path, spec: str = "HEAD") -> str:
    """The commit `spec` names in the git checkout at `path` (an annotated tag
    is peeled), or "" when it names none or `path` is not a checkout of its
    own. Reads the checkout's object store with `git rev-parse`; never runs
    anything from the checkout. Never raises."""
    if spec != "HEAD" and not _REF.fullmatch(spec):
        return ""
    try:
        if not (path / ".git").exists():
            return ""
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
        }
        # Never let git walk up out of `path` into an enclosing repository.
        env["GIT_CEILING_DIRECTORIES"] = str(path.resolve().parent)
        done = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{spec}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and _COMMIT.fullmatch(sha) else ""


def checkout_at(path: Path, ref: str) -> bool:
    """True when the checkout at `path` is at the commit `ref` names: the SDK
    source is labelled with the pin of the head under review only when it is
    that pin's source (FND-5316). A push between the step that chose the
    checkout's ref and the review can change the pin."""
    head = checkout_commit(path)
    return bool(head) and head == checkout_commit(path, ref)
