"""The gate-file fact: a changed bootstrap-managed file that differs from the render (FND-5325).

`atlan-application-sdk-conformance bootstrap` writes a set of CI files into
every connector repo and overwrites them on each resync: the managed workflow
shims (the reviewer's own `connector-reviewer.yaml` among them), the vendored
composite action and scripts. A hand edit to one of them in an app is lost at
the next resync, or holds the resync back, and it changes a CI gate that is
meant to be the same across the fleet. The place to change one is
application-sdk.

So when a pull request changes one of these gate files, the reviewer compares
it with what bootstrap renders for that repository, and a mismatch is one
finding, computed in code: no model call, no skeptic, no verify. The
comparison is not done here. This package is standard library only, and the
renderer is the conformance package's.

**Where it runs.** A job of its own in the reusable workflow (`gate`), before
the review job and apart from it: it holds no secret and no App token, and its
own token only reads contents. That job is the only place third-party code
runs (the conformance package and its dependencies, installed from the index
the lock names); the review job, which holds the App token and the model key,
still runs nothing but this package.

1. `gate-ref` (`head_ref_output`) writes the pull request's head ref for a
   sparse checkout of the head's CI directories, read as data. The
   repository's default branch is checked out too, `uv.lock` alone.
2. `gate-fact` (`make_fact`) reads the conformance version the BASE branch's
   `uv.lock` pins (`conformance_pin`; PyPI or its mirror only, a released
   version only), installs exactly that version into a fresh venv with
   `--only-binary :all:` (no build step runs), and runs `gate_render.py` with
   that venv's interpreter, `-I`, from outside the checkout. That script is
   the C002 drift check of that version, called as it is, over the head's
   files. It also reads the index's version list for the newest release (no
   install). The result is the fact file, uploaded as an artifact. Anything
   that goes wrong is a fact with `status: unavailable` and a reason, never a
   mismatch.
3. The review job reads the fact (`load_fact`, validated strictly: it came
   from a job that read pull-request files) and uses it only for the head it
   reviews and the conformance version the pull request's base pins
   (`evaluate`; the head's own pin is never read). Each round recomputes the
   findings, and `reconcile` closes a gate finding whose file now matches or
   is no longer changed. A round with no usable fact leaves earlier gate
   findings as they are and says why.

Why the base's pin: files are judged against what the branch they merge into
installs. A pull request that bumps the pin and resyncs is a dependency or
resync pull request, and exempt (`exempt.py`).

A file is flagged only when all of these hold: it exists on the base (a file
the pull request adds, or renames onto a managed name, never is); bootstrap
manages it at the base's pinned version, or it is on `UNMANAGED_FALLBACK` and
that version does not manage it yet; and the pull request's copy differs from
the render (for the fallback, from its base copy, `_fallback`) or is deleted.
Which files bootstrap manages: that version's C002 list
(`bootstrap_drift.discover`) without the two write-if-absent scaffolds the app
owns, `tests.yaml` and `renovate.json`. The repository's own
`.github/connector-reviewer/principles/` and any workflow bootstrap does not
write are never gate files, edited or deleted. One finding per file. When the
base's pin is older than the newest release on its index, the summary has one
note suggesting a bump; it never changes the status.

The finding is high: the status is red for any open finding, and what high
adds is that the pull request's author cannot dismiss it alone. A local edit to
a fleet CI gate is a decision for someone else to accept, and the two real
fixes (the change in application-sdk, or a resync) are cheap. No fleet
principle covers bootstrap-managed files, so it cites none.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from . import sdk_source
from .diff import FileDiff
from .finalize import pr_number
from .findings import Finding, PRState
from .select import DEFAULT_EXCLUDE, excluded
from .tools import secret_path

CONFORMANCE = "atlan-application-sdk-conformance"
FACT_ENV = "CONNECTOR_REVIEWER_GATE_FACT"
SCHEMA = 1
ORIGIN = "gate"
SEVERITY = "high"
FIXED_BY = "gate"  # `Finding.fixed_by` of a gate finding whose file now matches
RENDER_SCRIPT = Path(__file__).resolve().with_name("gate_render.py")

# Exactly the versions the conformance package releases.
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:rc\d+)?")
_COMMIT = re.compile(r"[0-9a-f]{40}")
# What a gate path may look like in the fact: plain repo-relative characters.
_PATH = re.compile(r"[A-Za-z0-9._/-]{1,200}")
KINDS = ("drifted", "absent", "retired")
# Gate files application-sdk manages that a pinned conformance version may not
# manage yet (FND-5325): the reviewer's own shim joined MANAGED_WORKFLOWS after
# conformance 0.60.0. When the pinned version's list lacks one, a pull request
# that changes or deletes it, compared with its base, is one finding all the
# same (`_fallback`). Once the pinned version manages it, the render decides.
# Fixed in code on purpose: nothing a pull request writes can add to it.
UNMANAGED_FALLBACK: tuple[str, ...] = (".github/workflows/connector-reviewer.yaml",)
UNMANAGED = "unmanaged"  # the kind of a fallback mismatch; never read from a fact
MAX_FILES = 100  # gate paths read from a fact
DIFF_KEEP = 2000  # characters of one file's diff read from a fact
MAX_FACT_BYTES = 1_000_000
VENV_SECONDS = 120
PIP_SECONDS = 300
RENDER_SECONDS = 120
LATEST_SECONDS = 10.0  # the index's version list, read once per gate job
LATEST_BYTES = 2_000_000

TEMPLATES_DIR = "packages/conformance/conformance/bootstrap/templates/"
BOOTSTRAP = "atlan-application-sdk-conformance bootstrap"


# ---- the gate job ------------------------------------------------------------


def head_ref_output(event_name: str, event: dict[str, Any], pr: int = 0) -> str:
    """The `$GITHUB_OUTPUT` line naming the pull request's head ref for the
    gate job's checkout: `ref=refs/pull/<n>/head`, or `ref=` when there is no
    pull request. The number is an int by construction, so nothing else can
    reach the checkout's `ref`."""
    number = pr if pr > 0 else pr_number(event_name, event)
    return f"ref=refs/pull/{number}/head\n" if number > 0 else "ref=\n"


@dataclass(frozen=True)
class Pin:
    """The conformance version a `uv.lock` pins, and the index it came from."""

    version: str
    index: str


def conformance_pin(lock_text: str) -> Pin | str:
    """The conformance package a `uv.lock` pins, or why there is none to use:
    absent, pinned more than one way, from a source other than PyPI or its
    mirror (another index could serve other contents under the same version),
    or a version that is not a release. Never raises."""
    entries = sdk_source.lock_entries(lock_text)
    if isinstance(entries, str):
        return entries
    found = [e for e in entries if e.get("name") == CONFORMANCE]
    if not found:
        return f"{CONFORMANCE} is not in uv.lock"
    pins: set[tuple[str, str]] = set()
    for entry in found:
        version, raw = entry.get("version"), entry.get("source")
        if not isinstance(version, str) or not isinstance(raw, dict):
            return f"{CONFORMANCE} in uv.lock has no version or source"
        source = cast(dict[str, object], raw)
        index = sdk_source.known_index(source.get("registry"))
        if index is None:
            return f"{CONFORMANCE} does not come from PyPI or its mirror"
        if not _VERSION.fullmatch(version):
            return f"{CONFORMANCE} {version[:40]!r} is not a released version"
        pins.add((version, index))
    if len(pins) > 1:
        return f"uv.lock pins {CONFORMANCE} more than one way"
    version, index = pins.pop()
    return Pin(version, index)


def _release_key(version: str) -> tuple[int, ...] | None:
    """A final release's version as numbers, or None (a pre-release, or not
    a version). Only final releases are suggested as a bump."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return None
    return tuple(int(x) for x in version.split("."))


Fetch = Callable[[str], "bytes | None"]


def _fetch(url: str) -> bytes | None:
    """The index's JSON simple page for the package (PEP 691), or None."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.pypi.simple.v1+json",
            "User-Agent": "connector-reviewer/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=LATEST_SECONDS) as r:
            return r.read(LATEST_BYTES)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def latest_release(index: str, fetch: Fetch = _fetch) -> str:
    """The newest final release of the conformance package on `index` (one of
    the known indexes, from `conformance_pin`), from the index's version list
    alone: no install. "" when it cannot be read. Never raises."""
    raw = fetch(f"{index}/{CONFORMANCE}/")
    if raw is None:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    versions = (
        cast(dict[str, object], data).get("versions")
        if isinstance(data, dict)
        else None
    )
    if not isinstance(versions, list):
        return ""
    best: tuple[tuple[int, ...], str] | None = None
    for v in cast(list[object], versions):
        key = _release_key(v) if isinstance(v, str) else None
        if key is not None and (best is None or key > best[0]):
            best = (key, cast(str, v))
    return best[1] if best else ""


def _clean_env() -> dict[str, str]:
    """The environment for the venv, pip and the render: no token, no PYTHON*
    or PIP* variable from the job."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "RUNNER_TEMP")
    return {k: os.environ[k] for k in keep if k in os.environ}


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _unavailable(reason: str, head: str = "", version: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"schema": SCHEMA, "status": "unavailable", "reason": reason}
    if head:
        out["head"] = head
    if version:
        out["conformance"] = version
    return out


def _step(
    run: Runner, argv: list[str], cwd: Path, seconds: float, what: str
) -> str | None:
    """Run one subprocess; the reason it failed, or None."""
    try:
        done = run(
            argv,
            cwd=str(cwd),
            env=_clean_env(),
            capture_output=True,
            text=True,
            timeout=seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"{what} timed out after {int(seconds)}s"
    except OSError as e:
        return f"{what} could not start ({type(e).__name__})"
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or [""]
        print(f"connector-reviewer: {what} failed: {tail[0][:300]}", file=sys.stderr)
        return f"{what} failed (exit {done.returncode})"
    return None


def make_fact(
    checkout: Path,
    venv: Path,
    base_checkout: Path,
    *,
    python: str = sys.executable,
    run: Runner = subprocess.run,
    script: Path = RENDER_SCRIPT,
    fetch: Fetch = _fetch,
) -> dict[str, Any]:
    """The fact for the pull request head checked out at `checkout`,
    rendered at the conformance version the base branch pins (`uv.lock` of
    `base_checkout`, the repository's default branch). Never raises: whatever
    goes wrong is `status: unavailable` with a reason.

    The base's pin, not the head's: the files are judged against what the
    branch they merge into would install. A pull request that bumps the pin
    and resyncs is a dependency or resync pull request, and exempt."""
    # Absolute: every subprocess runs from the venv's parent directory.
    checkout, venv = Path(os.path.abspath(checkout)), Path(os.path.abspath(venv))
    base_checkout = Path(os.path.abspath(base_checkout))
    head = sdk_source.checkout_commit(checkout)
    if not head:
        return _unavailable("the pull request head was not checked out")
    base = sdk_source.checkout_commit(base_checkout)
    if not base:
        return _unavailable("the base branch was not checked out", head)
    lock = base_checkout / sdk_source.LOCK
    try:
        if lock.is_symlink() or not lock.is_file():
            return _unavailable("the base branch has no uv.lock", head)
        text = lock.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _unavailable(
            f"the base branch's uv.lock could not be read ({type(e).__name__})", head
        )
    pin = conformance_pin(text)
    if isinstance(pin, str):
        return _unavailable(pin, head)
    work = venv.parent
    work.mkdir(parents=True, exist_ok=True)
    failed = _step(
        run, [python, "-m", "venv", str(venv)], work, VENV_SECONDS, "creating the venv"
    )
    vpy = str(venv / "bin" / "python")
    failed = failed or _step(
        run,
        [
            vpy,
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--quiet",
            "--only-binary",
            ":all:",
            "--index-url",
            pin.index,
            f"{CONFORMANCE}=={pin.version}",
        ],
        work,
        PIP_SECONDS,
        f"installing {CONFORMANCE} {pin.version}",
    )
    raw = work / "gate-render.json"
    failed = failed or _step(
        run,
        [vpy, "-I", str(script), "--root", str(checkout.resolve()), "--out", str(raw)],
        work,
        RENDER_SECONDS,
        "the render",
    )
    if failed:
        return _unavailable(failed, head, pin.version)
    try:
        rendered = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return _unavailable(
            f"the render wrote no result ({type(e).__name__})", head, pin.version
        )
    if not isinstance(rendered, dict):
        return _unavailable("the render wrote no result", head, pin.version)
    result = cast(dict[str, Any], rendered)
    if result.get("status") != "ok":
        return _unavailable(
            str(result.get("reason") or "the render did not compare")[:300],
            head,
            pin.version,
        )
    if result.get("conformance") != pin.version:
        return _unavailable(
            "the installed conformance version is not the pinned one", head, pin.version
        )
    out = {
        **result,
        "schema": SCHEMA,
        "head": head,
        "base": base,
        "conformance": pin.version,
    }
    latest = latest_release(pin.index, fetch)
    if latest:
        out["latest"] = latest
    return out


# ---- the review job ----------------------------------------------------------


@dataclass(frozen=True)
class Mismatch:
    kind: str  # one of KINDS
    added: int = 0
    removed: int = 0
    diff: str = ""
    cut: bool = False


@dataclass
class Fact:
    head: str
    conformance: str
    gate_files: frozenset[str]
    mismatches: dict[str, Mismatch] = field(default_factory=dict)
    # The newest final release on the pin's index, "" when it was not read.
    latest: str = ""
    unchecked: dict[str, str] = field(default_factory=dict)


def _safe_path(raw: object) -> str | None:
    if not isinstance(raw, str) or not _PATH.fullmatch(raw):
        return None
    # Split by hand: PurePosixPath drops a `.` segment and an empty one.
    if any(p in ("", ".", "..") for p in raw.split("/")):
        return None
    return raw


def _count(raw: object) -> int:
    return raw if isinstance(raw, int) and 0 <= raw < 1_000_000 else 0


def load_fact(path: str) -> tuple[Fact | None, str]:
    """The fact at `path`, or None and why it cannot be used. Read strictly:
    it was written by a job that read pull-request files."""
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_file():
            return None, "the gate check did not run"
        if p.stat().st_size > MAX_FACT_BYTES:
            return None, "the gate check's result is too large"
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, "the gate check's result could not be read"
    if not isinstance(raw, dict):
        return None, "the gate check's result could not be read"
    data = cast(dict[str, Any], raw)
    if data.get("schema") != SCHEMA:
        return None, "the gate check's result has another format"
    version = data.get("conformance")
    shown = (
        f" (conformance {version})"
        if isinstance(version, str) and _VERSION.fullmatch(version)
        else ""
    )
    if data.get("status") != "ok":
        reason = str(data.get("reason") or "no reason given")
        reason = re.sub(r"[^\w .,:;()'`=/@-]", "", reason)[:200]
        return None, f"the gate check was not made{shown}: {reason}"
    head = data.get("head")
    if not isinstance(head, str) or not _COMMIT.fullmatch(head):
        return None, "the gate check's result names no head"
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        return None, "the gate check's result names no conformance version"
    files_raw = data.get("gate_files")
    if not isinstance(files_raw, list):
        return None, "the gate check's result lists no gate files"
    files = {
        s
        for s in (_safe_path(x) for x in cast(list[object], files_raw)[:MAX_FILES])
        if s
    }
    found, unchecked = data.get("mismatches"), data.get("unchecked")
    # Both are required: a file with no entry in either reads as matching the
    # render, so a fact that leaves them out must not be read as all clean.
    if not isinstance(found, dict) or not isinstance(unchecked, dict):
        return None, "the gate check's result has no mismatch or unchecked list"
    latest = data.get("latest")
    fact = Fact(
        head=head,
        conformance=version,
        gate_files=frozenset(files),
        latest=latest if isinstance(latest, str) and _release_key(latest) else "",
    )
    if found:
        for k, v in list(cast(dict[object, object], found).items())[:MAX_FILES]:
            rel = _safe_path(k)
            if rel is None or rel not in files or not isinstance(v, dict):
                continue
            m = cast(dict[str, object], v)
            kind = m.get("kind")
            if kind not in KINDS:
                continue
            diff = m.get("diff")
            fact.mismatches[rel] = Mismatch(
                kind=str(kind),
                added=_count(m.get("added")),
                removed=_count(m.get("removed")),
                diff=diff[:DIFF_KEEP] if isinstance(diff, str) else "",
                cut=m.get("cut") is True,
            )
    if unchecked:
        for k, v in list(cast(dict[object, object], unchecked).items())[:MAX_FILES]:
            rel = _safe_path(k)
            if rel is not None and rel in files:
                fact.unchecked[rel] = re.sub(r"[^\w .,:;()-]", "", str(v))[:80]
    return fact, ""


def added_paths(files: list[FileDiff]) -> set[str]:
    """The paths the pull request creates: new files, and a rename's new
    path. Neither exists on the base."""
    return {
        fd.path
        for fd in files
        if fd.status == "added" or (fd.old_path is not None and fd.old_path != fd.path)
    }


def changed_paths(files: list[FileDiff]) -> list[str]:
    """Every path the pull request changes, a rename's old path included."""
    out: list[str] = []
    for fd in files:
        for p in (fd.path, fd.old_path):
            if p and p not in out:
                out.append(p)
    return out


def _body(m: Mismatch, version: str) -> str:
    pinned = f"conformance {version} (the version the base branch's `uv.lock` pins)"
    managed = (
        f"`{BOOTSTRAP}` writes this file and overwrites it on every resync, and the "
        "app's Conformance gate (C002) compares it with the render, so a local change "
        "is lost at the next resync or holds the resync back."
    )
    if m.kind == "absent":
        what = f"This pull request deletes a bootstrap-managed file that {pinned} still installs."
        fix = (
            f"Restore it with a resync (`{BOOTSTRAP}`), or retire it in application-sdk "
            "(`RETIRED_FILES` in `packages/conformance/conformance/bootstrap/render.py`)."
        )
    elif m.kind == UNMANAGED:
        what = (
            "This file is managed by application-sdk, and this pull request "
            + (
                "deletes it."
                if m.removed and not m.added and not m.diff
                else f"changes it from its base copy (+{m.added} −{m.removed} line(s))."
            )
            + f" Conformance {version}, the version the base branch's `uv.lock` "
            "pins, does not manage it yet, so it is compared with the base branch "
            "instead of a render."
        )
        managed = (
            "A local change to it is lost at the next resync that installs it, and it "
            "changes a gate meant to be the same across the fleet."
        )
        fix = (
            "It is managed by application-sdk: change it there "
            f"(`{TEMPLATES_DIR}`), or run a resync (`{BOOTSTRAP}`)."
        )
    elif m.kind == "retired":
        what = f"This pull request changes a file that {pinned} has retired: bootstrap removes it."
        fix = (
            f"Run a resync (`{BOOTSTRAP}`) to remove it, or bring it back in application-sdk "
            "if it is still needed."
        )
    else:
        what = (
            f"It differs from what {pinned} renders for this repository: "
            f"+{m.added} −{m.removed} line(s), action pins ignored."
        )
        fix = (
            f"Edit it in application-sdk (`{TEMPLATES_DIR}`), or run a resync "
            f"(`{BOOTSTRAP}`) to put back the render."
        )
    return f"{what} {managed} {fix}"


def _title(rel: str, kind: str) -> str:
    if kind == UNMANAGED:
        return f"`{rel}` is managed by application-sdk and changed here"
    if kind == "absent":
        return f"Bootstrap-managed `{rel}` is deleted"
    if kind == "retired":
        return f"Retired bootstrap file `{rel}` is changed"
    return f"Bootstrap-managed `{rel}` differs from the render"


def finding_for(
    rel: str, m: Mismatch, version: str, exclude: tuple[str, ...] = DEFAULT_EXCLUDE
) -> Finding:
    """One finding for one gate file. Its id comes from the path alone (the
    quote is a fixed `gate:<path>`), so an edit to the file, or a change of
    kind, keeps the same finding. The diff is shown only for a file whose diff
    a prompt may carry (`select.model_visible`'s rule: never a path that may
    hold secrets, never an excluded one)."""
    visible = not secret_path(rel) and not excluded(rel, exclude)
    diff = m.diff if visible and m.kind in ("drifted", UNMANAGED) else ""
    if diff and m.cut:
        diff += "\n…"
    return Finding(
        path=rel,
        line=0,
        severity=SEVERITY,
        category="other",
        title=_title(rel, m.kind),
        body=_body(m, version),
        evidence=f"gate:{rel}",
        suggestion=diff,
        origin=ORIGIN,
    )


@dataclass
class Evaluation:
    """What a round's gate check found. `findings` is None when there was no
    usable fact, and then earlier gate findings are left as they are."""

    findings: list[Finding] | None
    notes: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)  # changed gate files compared
    matching: list[str] = field(default_factory=list)  # of those, the ones that match
    # Changed gate files the render did not compare (a symlink, an unreadable
    # or oversized file): an open gate finding on one is left as it is.
    unchecked: list[str] = field(default_factory=list)
    # Gate-file names the pull request adds (absent on its base): not flagged.
    added: list[str] = field(default_factory=list)


Reader = Callable[[str], "str | None"]


def _normal(text: str) -> str:
    """Line endings normalised, as C002 reads a file (universal newlines)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _fallback(
    rel: str, files: list[FileDiff], read_base: Reader, read_head: Reader
) -> tuple[Mismatch | None, str]:
    """A file on `UNMANAGED_FALLBACK` the pinned version does not manage,
    compared by content with its base copy (both read through the API, as
    data): the mismatch, or None and a note when it cannot be compared."""
    deleted = any(
        (fd.path == rel and fd.status == "deleted")
        or (fd.old_path == rel and fd.path != rel)
        for fd in files
    )
    base = read_base(rel)
    if base is None:
        return None, (
            f"Gate-file check: `{rel}` was not compared: its base copy could not "
            "be read."
        )
    if deleted:
        return Mismatch(UNMANAGED, 0, len(base.splitlines())), ""
    now = read_head(rel)
    if now is None:
        return None, f"Gate-file check: `{rel}` could not be read at the head."
    old, new = _normal(base).splitlines(), _normal(now).splitlines()
    if old == new:
        return None, ""
    lines = list(
        difflib.unified_diff(old, new, "base branch", "pull request", n=1, lineterm="")
    )[2:]
    shown: list[str] = []
    for ln in lines[:24]:
        if sum(len(x) + 1 for x in shown) + len(ln) + 1 > 1500:
            break
        shown.append(ln)
    return (
        Mismatch(
            UNMANAGED,
            added=sum(1 for ln in lines if ln.startswith("+")),
            removed=sum(1 for ln in lines if ln.startswith("-")),
            diff="\n".join(shown),
            cut=len(shown) < len(lines),
        ),
        "",
    )


def evaluate(
    path: str | None,
    files: list[FileDiff],
    *,
    head: str,
    base_lock_text: str | None,
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
    read_base: Reader | None = None,
    read_head: Reader | None = None,
) -> Evaluation:
    """The gate findings of the pull request whose whole diff is `files`, at
    `head`, from the fact at `path`. `base_lock_text` is the `uv.lock` of
    the pull request's base: the fact counts only when it rendered the
    conformance version that lock pins (the head's own pin is not read).
    `path` None means the gate check is not configured (a local run): no note.
    `read_base` and `read_head` read a file's text at the base and the head;
    with both, a file on `UNMANAGED_FALLBACK` that the pinned version does not
    manage is compared with its base copy instead (`_fallback`)."""
    if path is None:
        return Evaluation(None)
    fact, why = load_fact(path)
    if fact is None:
        return Evaluation(None, [f"Gate-file check not made: {why}."])
    if fact.head != head:
        return Evaluation(
            None,
            [
                f"Gate-file check not made: it read head {fact.head[:9]}, and this "
                f"review reads {head[:9]}; `/review force` checks again."
            ],
        )
    pin = (
        conformance_pin(base_lock_text) if base_lock_text is not None else "no uv.lock"
    )
    if isinstance(pin, str) or pin.version != fact.conformance:
        return Evaluation(
            None,
            [
                "Gate-file check not made: it rendered conformance "
                f"{fact.conformance}, which is not the version the pull request's "
                "base pins."
            ],
        )
    # Only a hand edit or deletion of a file that exists on the base counts.
    # A file the pull request adds (or renames onto a managed name) is never
    # flagged: nothing bootstrap installed there was edited, and the next
    # resync writes it anyway.
    added = added_paths(files)
    touched = changed_paths(files)
    changed = [p for p in touched if p in fact.gate_files and p not in added]
    found: list[Finding] = []
    out = Evaluation(found, checked=changed)
    out.added = [
        p
        for p in touched
        if p in added and (p in fact.gate_files or p in UNMANAGED_FALLBACK)
    ]
    newest, pinned = _release_key(fact.latest), _release_key(fact.conformance)
    if newest and pinned and newest > pinned:
        out.notes.append(
            f"Pinned conformance {fact.conformance} is behind {fact.latest}; bump it "
            "with a resync to pick up newer managed files."
        )
    for rel in changed:
        if rel in fact.unchecked:
            out.notes.append(
                f"Gate-file check: `{rel}` was not compared ({fact.unchecked[rel]})."
            )
            out.unchecked.append(rel)
            continue
        m = fact.mismatches.get(rel)
        if m is None:
            out.matching.append(rel)
        else:
            found.append(finding_for(rel, m, fact.conformance, exclude))
    if read_base is None or read_head is None:
        return out
    for rel in UNMANAGED_FALLBACK:
        if rel in fact.gate_files or rel not in touched or rel in added:
            continue  # the render decides, the PR leaves it alone, or adds it
        out.checked.append(rel)
        m, note = _fallback(rel, files, read_base, read_head)
        if note:
            out.notes.append(note)
            out.unchecked.append(rel)
        elif m is None:
            out.matching.append(rel)
        else:
            found.append(finding_for(rel, m, fact.conformance, exclude))
    return out


def reconcile(
    state: PRState,
    fresh: list[Finding] | None,
    round_no: int,
    unchecked: list[str] | tuple[str, ...] = (),
) -> tuple[list[Finding], list[str]]:
    """Fold a round's gate findings into the state: the new ones (a reopened
    one included) and the ids closed because their file now matches the render
    or is no longer changed. `fresh` None (no usable fact) changes nothing, and
    an open finding on an `unchecked` path (one the render did not compare) is
    left open. A dismissed gate finding stays dismissed."""
    if fresh is None:
        return [], []
    by_id = {f.id: f for f in fresh}
    new: list[Finding] = []
    resolved: list[str] = []
    for old in state.findings:
        if old.origin != ORIGIN:
            continue
        now = by_id.pop(old.id, None)
        if now is None:
            if old.status == "open" and old.path not in unchecked:
                old.status, old.fixed_round, old.fixed_by = "fixed", round_no, FIXED_BY
                resolved.append(old.id)
            continue
        if old.status == "open" or old.fixed_by == FIXED_BY:
            reopened = old.status != "open"
            old.title, old.body, old.suggestion = now.title, now.body, now.suggestion
            old.severity = now.severity
            if reopened:
                old.status, old.fixed_round, old.fixed_by = "open", 0, ""
                old.round = round_no
                new.append(old)
    for f in by_id.values():
        f.round = round_no
        state.findings.append(f)
        new.append(f)
    return new, resolved
