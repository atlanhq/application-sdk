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
   sparse checkout of the head: the CI directories and `uv.lock`, read as data.
2. `gate-fact` (`make_fact`) reads the conformance version that head's
   `uv.lock` pins (`conformance_pin`; PyPI or its mirror only, a released
   version only), installs exactly that version into a fresh venv with
   `--only-binary :all:` (no build step runs), and runs `gate_render.py` with
   that venv's interpreter, `-I`, from outside the checkout. That script is
   the C002 drift check of the pinned version, called as it is. The result is
   the fact file, uploaded as an artifact. Anything that goes wrong is a fact
   with `status: unavailable` and a reason, never a mismatch.
3. The review job reads the fact (`load_fact`, validated strictly: it came
   from a job that read pull-request files) and uses it only for the head it
   reviews and the conformance version that head pins (`evaluate`). Every
   mismatch on a file the whole pull request changes (a rename's old path
   included) is one finding (`finding_for`). Each round recomputes them, and
   `reconcile` closes a gate finding whose file now matches or is no longer
   changed. A round with no usable fact leaves earlier gate findings as they
   are and says why.

Which files: the pinned version's C002 list (`bootstrap_drift.discover`)
without the two write-if-absent scaffolds the app owns, `tests.yaml` and
`renovate.json`. The repository's own `.github/connector-reviewer/principles/`
and any workflow bootstrap does not write are not gate files.

The finding is high: the status is red for any open finding, and what high
adds is that the pull request's author cannot dismiss it alone. A local edit to
a fleet CI gate is a decision for someone else to accept, and the two real
fixes (the change in application-sdk, or a resync) are cheap. No fleet
principle covers bootstrap-managed files, so it cites none.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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
MAX_FILES = 100  # gate paths read from a fact
DIFF_KEEP = 2000  # characters of one file's diff read from a fact
MAX_FACT_BYTES = 1_000_000
VENV_SECONDS = 120
PIP_SECONDS = 300
RENDER_SECONDS = 120

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
    *,
    python: str = sys.executable,
    run: Runner = subprocess.run,
    script: Path = RENDER_SCRIPT,
) -> dict[str, Any]:
    """The fact for the pull request head checked out at `checkout`. Never
    raises: whatever goes wrong is `status: unavailable` with a reason."""
    head = sdk_source.checkout_commit(checkout)
    if not head:
        return _unavailable("the pull request head was not checked out")
    lock = checkout / sdk_source.LOCK
    try:
        if lock.is_symlink() or not lock.is_file():
            return _unavailable("the pull request head has no uv.lock", head)
        text = lock.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _unavailable(f"uv.lock could not be read ({type(e).__name__})", head)
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
    return {**result, "schema": SCHEMA, "head": head, "conformance": pin.version}


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
    fact = Fact(head=head, conformance=version, gate_files=frozenset(files))
    found = data.get("mismatches")
    if isinstance(found, dict):
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
    unchecked = data.get("unchecked")
    if isinstance(unchecked, dict):
        for k, v in list(cast(dict[object, object], unchecked).items())[:MAX_FILES]:
            rel = _safe_path(k)
            if rel is not None and rel in files:
                fact.unchecked[rel] = re.sub(r"[^\w .,:;()-]", "", str(v))[:80]
    return fact, ""


def changed_paths(files: list[FileDiff]) -> list[str]:
    """Every path the pull request changes, a rename's old path included."""
    out: list[str] = []
    for fd in files:
        for p in (fd.path, fd.old_path):
            if p and p not in out:
                out.append(p)
    return out


def _body(m: Mismatch, version: str) -> str:
    pinned = f"conformance {version} (the version this pull request's `uv.lock` pins)"
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
    diff = m.diff if visible and m.kind == "drifted" else ""
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


def evaluate(
    path: str | None,
    files: list[FileDiff],
    *,
    head: str,
    lock_text: str | None,
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
) -> Evaluation:
    """The gate findings of the pull request whose whole diff is `files`, at
    `head` (whose `uv.lock` text is `lock_text`), from the fact at `path`.
    `path` None means the gate check is not configured (a local run): no note."""
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
    pin = conformance_pin(lock_text) if lock_text is not None else "no uv.lock"
    if isinstance(pin, str) or pin.version != fact.conformance:
        return Evaluation(
            None,
            [
                "Gate-file check not made: it rendered conformance "
                f"{fact.conformance}, which is not the version this head pins."
            ],
        )
    changed = [p for p in changed_paths(files) if p in fact.gate_files]
    found: list[Finding] = []
    out = Evaluation(found, checked=changed)
    for rel in changed:
        if rel in fact.unchecked:
            out.notes.append(
                f"Gate-file check: `{rel}` was not compared ({fact.unchecked[rel]})."
            )
            continue
        m = fact.mismatches.get(rel)
        if m is None:
            out.matching.append(rel)
        else:
            found.append(finding_for(rel, m, fact.conformance, exclude))
    return out


def reconcile(
    state: PRState, fresh: list[Finding] | None, round_no: int
) -> tuple[list[Finding], list[str]]:
    """Fold a round's gate findings into the state: the new ones (a reopened
    one included) and the ids closed because their file now matches the render
    or is no longer changed. `fresh` None (no usable fact) changes nothing. A
    dismissed gate finding stays dismissed."""
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
            if old.status == "open":
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
