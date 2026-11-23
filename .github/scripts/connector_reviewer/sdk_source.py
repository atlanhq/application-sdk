"""Which application-sdk the app under review pins, as a git ref (FND-5331).

The reviewer can read the Atlan Application SDK source at the exact version the
app pins, so it can check how an SDK API the diff calls behaves instead of
judging it from memory. This module answers one question from the app's
`uv.lock` (on the trusted base checkout, never the pull request's): which ref of
`atlanhq/application-sdk` to check out.

- A registry pin from PyPI, or the org's registry firewall that mirrors it
  (`_KNOWN_REGISTRIES`; any other index falls back), of version
  `X.Y.Z` or `X.Y.ZrcN` is the release tag `vX.Y.Z` / `vX.Y.ZrcN`, the tag scheme
  application-sdk's releases use.
- A git pin of `github.com/atlanhq/application-sdk` is the locked commit: uv
  writes it after `#` in the source URL, whatever `rev=`/`branch=`/`tag=` says.
- Anything else is no ref: the package absent, more than one distinct pin, an
  editable or path source, a git pin of another repository (its commit is not in
  atlanhq/application-sdk), a version with no release tag, or a lock that does
  not parse. The review then runs without the SDK source and says so.

The workflow calls `python -m connector_reviewer sdk-ref --root <app checkout>`,
which writes the ref as a step output for the checkout step (`step_output`): the
decision is here, tested, and not in workflow shell (docs/standards/ci.md). The
tools that read the checkout are `tools.SdkSource`.
"""

from __future__ import annotations

import re
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import cast

PACKAGE = "atlan-application-sdk"
REPOSITORY = "atlanhq/application-sdk"
# The SDK checkout holds this directory only (a sparse checkout).
PACKAGE_DIR = "application_sdk"

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


def resolve(lock_text: str) -> Resolution:
    """The pinned application-sdk in a `uv.lock`'s text. Never raises."""
    try:
        lock = tomllib.loads(lock_text)
    except (tomllib.TOMLDecodeError, TypeError, ValueError) as e:
        return Resolution(None, f"uv.lock does not parse ({type(e).__name__})")
    packages = lock.get("package")
    if not isinstance(packages, list):
        return Resolution(None, f"{PACKAGE} is not in uv.lock")
    entries = [
        entry
        for entry in (
            cast(dict[str, object], p)
            for p in cast(list[object], packages)
            if isinstance(p, dict)
        )
        if entry.get("name") == PACKAGE
    ]
    if not entries:
        return Resolution(None, f"{PACKAGE} is not in uv.lock")
    pins: set[tuple[str, str, str]] = set()
    for entry in entries:
        one = _pin_of(entry)
        if isinstance(one, str):
            return Resolution(None, one)
        pins.add((one.ref, one.version, one.kind))
    if len(pins) > 1:
        return Resolution(None, f"uv.lock pins {PACKAGE} more than one way")
    ref, version, kind = pins.pop()
    return Resolution(SdkPin(ref=ref, version=version, kind=kind))


def _pin_of(entry: dict[str, object]) -> SdkPin | str:
    """One `[[package]]` entry as a pin, or the reason it is not one."""
    version = entry.get("version")
    raw_source = entry.get("source")
    if not isinstance(version, str) or not isinstance(raw_source, dict):
        return f"{PACKAGE} in uv.lock has no version or source"
    source = cast(dict[str, object], raw_source)
    if "registry" in source:
        if not _known_registry(source.get("registry")):
            # Another index could serve different contents under the same
            # version, so the release tag would not be what the app runs.
            return f"{PACKAGE} comes from a registry that is not PyPI or its mirror"
        if not _RELEASE.fullmatch(version):
            return f"{PACKAGE} {version!r} is not a released version with a tag"
        return SdkPin(ref=f"v{version}", version=version, kind="release")
    git = source.get("git")
    if isinstance(git, str):
        try:
            url = urllib.parse.urlsplit(git)
            host = url.hostname
        except ValueError:
            return f"{PACKAGE}'s git source in uv.lock is malformed"
        repo = url.path.strip("/").removesuffix(".git")
        if url.scheme != "https" or host != "github.com" or repo != REPOSITORY:
            return f"{PACKAGE} is pinned to a git source other than {REPOSITORY}"
        commit = url.fragment
        if not _COMMIT.fullmatch(commit):
            return f"{PACKAGE}'s git pin in uv.lock has no locked commit"
        shown = version if _VERSION.fullmatch(version) else ""
        return SdkPin(ref=commit, version=shown, kind="git")
    kinds = ", ".join(sorted(source)) or "empty"
    return f"{PACKAGE} comes from a {kinds} source, not a release or a git commit"


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


def step_output(res: Resolution) -> str:
    """The `$GITHUB_OUTPUT` lines for the checkout step: `ref` empty unless it is
    one of the two safe shapes, so the step's `if:` skips the checkout."""
    pin = res.pin
    ok = pin is not None and _REF.fullmatch(pin.ref) is not None
    ref = pin.ref if pin is not None and ok else ""
    label = pin.label if pin is not None and ok else ""
    if not _LABEL.fullmatch(label):
        label = ""
    return f"ref={ref}\nlabel={label}\n"


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
