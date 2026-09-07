#!/usr/bin/env python3
"""Layout-aware placement of ``pkl eval`` contract output into an app's tree.

Every regeneration entry point (``renovate_pkl_sync.py``,
``regenerate_contract.py``, and ``check_generated_freshness.py`` via its import
of the former) evaluates a consumer's ``contract/app.pkl`` and has to put the
result where the app actually keeps it. That placement is NOT uniform, because
the toolkit has two contract families that emit different output-key shapes:

  * **Prefixed family** — ``amends "@app-contract-toolkit/App.pkl"``. Keys are
    repo-root-relative and carry the ``app/generated/`` prefix::

        app/generated/manifest.json
        app/generated/crawler/_input.py
        atlan.yaml
        app.yaml

    See ``contract-toolkit/src/App.pkl`` (``["app/generated/manifest.json"] =
    …``). Consumers generate with ``pkl eval -m . contract/app.pkl`` from the
    repo root, so the output base IS the repo root.

  * **Native family** — ``amends "@app-contract-toolkit/NativeApp.pkl"`` or
    ``NativeAppBundle.pkl``. Keys are relative to the *generated dir* and carry
    no prefix::

        manifest.json                  (NativeApp)
        _input.py
        <workflowConfigName>.json
        atlan.yaml                     (NativeAppBundle — repo-root file)
        <entrypoint>/manifest.json     (NativeAppBundle)

    See ``NativeApp.pkl`` (``["manifest.json"] = manifestOutput``) and
    ``NativeAppBundle.pkl`` (``["\\(entrypoint.name)/\\(fileName)"]``).
    Consumers generate with ``pkl eval -m ../app/generated`` from ``contract/``
    and then move ``atlan.yaml``/``app.yaml`` up to the repo root.

Before this module every caller hard-coded the prefixed shape — it looked for
``<out_dir>/app/generated`` and, not finding it, silently did nothing. That made
regeneration a no-op for the entire native family (the majority of the fleet):
a Renovate toolkit bump merged with stale artifacts and a green check, the
freshness gate agreed it was fresh, and the pre-test regeneration restored the
committed manifest. This module is the single place that knows the difference.

Two deliberate asymmetries between the families, both about not destroying
files the toolkit does not own:

  * **Prefixed: replace the generated dir wholesale.** ``App.pkl`` emits every
    file under ``app/generated/`` including ``__init__.py``, so a
    rmtree+copytree is safe and clears orphans left by a removed or renamed
    entrypoint — with one carve-out, ``RESERVED_GENERATED_SUBDIRS`` below.

  * **Native: overwrite emitted files in place, never delete.** The generated
    dir here holds files pkl does not emit — e.g. the ``__init__.py`` an app's
    own generate task ``touch``es — and an unprefixed key set gives us no way to
    tell an app-owned file from a toolkit orphan. Deleting a committed
    ``__init__.py`` (or anything else the app maintains) to tidy up hypothetical
    orphans is the worse failure, so orphans are left for the human to notice in
    the diff.

An app whose generated dir is not the default (``app/generated``) — e.g. one
evaluating into ``app/generated/<variant>`` — is refused by ``swap_outputs``
rather than written to the wrong base; see ``_native_target_is_plausible``.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

# Repo-root files both families emit at the top level of the eval output. They
# live at the repo root, never inside the generated dir.
ROOT_FILES = ("atlan.yaml", "app.yaml")

# Where an app keeps its generated artifacts. The overwhelming default across
# the fleet; apps that differ are refused rather than mis-written.
GENERATED_DIR = "app/generated"

# Subdirectory names under the generated dir that NO pkl contract, in any
# family or toolkit version, ever emits into — so baseline/override detection
# (which compares what a contract emitted then vs. now) can never see them and
# the prefixed family's wholesale rmtree+copytree would otherwise delete them
# on every regeneration.
#
# ``"frontend"``: ``create_app_handler_service``'s ``frontend_assets_path``
# defaults to ``<generated_dir>/frontend/static``, populated by the
# ``@atlanhq/app-playground`` CLI, not by any contract. Re-running that CLI to
# restore the directory is not an option — its build output is not
# byte-reproducible (fresh content hashes and a random build UUID every run,
# confirmed against the same pinned package version), so a diff-based swap can
# never converge; the only stable behavior is to leave it untouched.
RESERVED_GENERATED_SUBDIRS = ("frontend",)

# Optional app-owned script run after a successful swap, relative to the contract
# dir. A convention rather than a per-workflow input on purpose: every
# regeneration entry point picks it up automatically, so they cannot disagree
# about what "freshly generated" means — an input would have to be threaded
# through (and kept in sync across) the renovate sync, the freshness gate and the
# pre-test regeneration, and any mismatch shows up as a gate reporting drift the
# sync would never produce.
POST_GENERATE_SCRIPT = "post-generate.sh"


def detect_layout(out_dir: Path) -> str:
    """Classify what ``pkl eval -m <out_dir>`` produced.

    Returns one of:

      * ``"prefixed"`` — ``<out_dir>/app/generated`` exists (``App.pkl`` family).
      * ``"native"``   — no such dir, but there is at least one emitted entry
        that is not a repo-root file (``NativeApp``/``NativeAppBundle`` family).
      * ``"root-only"`` — only ``atlan.yaml``/``app.yaml`` were emitted. A real
        contract of either family always emits generated artifacts too, so this
        means the output is partial.
      * ``"empty"`` — nothing at all.

    ``root-only`` and ``empty`` are both "no generated artifacts to place"; they
    are distinguished so callers can report the more specific one.
    """
    if (out_dir / "app" / "generated").is_dir():
        return "prefixed"
    entries = [p for p in out_dir.iterdir() if p.name not in ROOT_FILES]
    if entries:
        return "native"
    if any((out_dir / name).exists() for name in ROOT_FILES):
        return "root-only"
    return "empty"


def _native_target_is_plausible(entries: list[str], target: Path) -> bool:
    """Guard against writing native-family output to the wrong generated dir.

    The native families emit keys relative to whatever base the app passes to
    ``pkl eval -m``, and that base is not recoverable from the output — so
    ``GENERATED_DIR`` is an assumption. It holds for apps evaluating into
    ``app/generated`` and fails for one evaluating into a subdirectory of it
    (``app/generated/<variant>``, as a multi-variant connector does): there the
    emitted names would land a level too high, creating a set of wrong files
    beside the real ones.

    An empty or absent target is accepted — a first generation has nothing to
    compare against. Otherwise require at least one emitted top-level name to
    already exist in the target. That admits a contract *growing* a new
    entrypoint (some names overlap, the new one does not) while rejecting a base
    mismatch, where nothing overlaps because the target holds only variant
    directories.
    """
    if not target.is_dir():
        return True
    existing = {p.name for p in target.iterdir()}
    if not existing:
        return True
    return any(name in existing for name in entries)


def plan_swap(
    out_dir: Path, generated_dir: str = GENERATED_DIR
) -> tuple[str, dict[Path, Path]]:
    """Map every file in ``out_dir`` to where it belongs in the working tree.

    Returns ``(layout, {dest: src})``. Destinations are repo-relative, so the
    same call against a baseline eval output yields keys directly comparable with
    the working tree — that is what makes override detection possible.

    Root files map to the repo root; generated artifacts map under
    ``generated_dir`` with the family's prefix stripped. An empty mapping means
    there was nothing to place.
    """
    layout = detect_layout(out_dir)
    plan: dict[Path, Path] = {}

    for name in ROOT_FILES:
        src = out_dir / name
        if src.exists():
            plan[Path(name)] = src

    target = Path(generated_dir)
    base = out_dir / "app" / "generated" if layout == "prefixed" else out_dir
    if layout in ("prefixed", "native"):
        for src in sorted(base.rglob("*")):
            if src.is_dir():
                continue
            rel = src.relative_to(base)
            # In the native layout the root files sit alongside the generated
            # artifacts; they are already planned to the repo root above.
            if layout == "native" and len(rel.parts) == 1 and rel.name in ROOT_FILES:
                continue
            plan[target / rel] = src

    return layout, plan


def overridden_files(
    plan: dict[Path, Path], baseline_plan: dict[Path, Path]
) -> set[Path]:
    """Destinations the app maintains itself, so regeneration must not touch them.

    A file is app-owned when its committed content differs from what the toolkit
    produced for the *baseline* pin — the pin that the committed artifacts were
    generated from. Several apps install a hand-maintained artifact over the
    toolkit's output (a credential form the toolkit cannot yet express, a patched
    input contract); the committed file is therefore not what `pkl eval` emits,
    and a swap that overwrites it silently reverts the override and ships the
    unusable version.

    Detecting it this way needs no per-app declaration: the difference between
    "what the toolkit produced then" and "what is committed" IS the app's
    post-processing. Only destinations present in both plans are considered, so a
    newly-emitted file is never treated as an override.

    Caveat worth knowing: if the committed tree is *stale* against its own pin —
    a contract change merged without regenerating, which the freshness gate is
    meant to catch but does not block — that staleness also reads as an override
    and the file is preserved rather than refreshed. Callers therefore log every
    preserved path instead of silently skipping it.
    """
    overridden = set()
    for dest, baseline_src in baseline_plan.items():
        if dest not in plan or not dest.exists():
            continue
        if dest.read_bytes() != baseline_src.read_bytes():
            overridden.add(dest)
    return overridden


def _withhold_reserved_subdirs(target: Path) -> dict[str, Path]:
    """Move each existing ``RESERVED_GENERATED_SUBDIRS`` entry out of ``target``
    into a temp holding dir, ahead of a wholesale rmtree.

    Returns ``{name: backup_path}`` for every subdir that existed, to be handed
    to ``_restore_reserved_subdirs``. Never raises: a reserved name that is not
    a directory (or is absent) is simply skipped.
    """
    backups: dict[str, Path] = {}
    for name in RESERVED_GENERATED_SUBDIRS:
        src = target / name
        if src.is_dir():
            holding = Path(tempfile.mkdtemp())
            backup = holding / name
            shutil.move(str(src), str(backup))
            backups[name] = backup
    return backups


def _restore_reserved_subdirs(target: Path, backups: dict[str, Path]) -> None:
    """Move withheld reserved subdirs back under ``target`` and clean up.

    Skips (and warns on) a name the fresh copytree unexpectedly recreated —
    e.g. a future contract legitimately emitting that name — rather than
    silently overwriting it; that would only happen if a contract started
    emitting a key under a reserved name, which is a real change to surface,
    not paper over.
    """
    for name, backup in backups.items():
        dest = target / name
        if dest.exists():
            print(
                f"::warning::pkl eval emitted '{name}' under the generated dir, "
                "which collides with a reserved name normally left untouched "
                f"(RESERVED_GENERATED_SUBDIRS); keeping the freshly emitted "
                f"'{name}' and discarding the withheld copy."
            )
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(dest))
        shutil.rmtree(backup.parent, ignore_errors=True)


def swap_outputs(
    out_dir: Path,
    generated_dir: str = GENERATED_DIR,
    baseline_dir: Path | None = None,
) -> bool:
    """Move eval output from ``out_dir`` into the working tree. Returns True iff
    generated artifacts were actually placed.

    Repo-root files (``atlan.yaml``/``app.yaml``) are copied for either family
    whenever emitted. The generated artifacts are placed per the family rules in
    this module's docstring.

    ``baseline_dir`` is the eval output for the pin the committed artifacts were
    generated from. When given, files the app post-processes are detected and
    preserved — see ``overridden_files``. Omit it (the default) to overwrite
    everything the eval emitted.

    In the prefixed family, any existing ``RESERVED_GENERATED_SUBDIRS`` entry
    (e.g. ``frontend/``) survives the wholesale replace unconditionally — no
    baseline needed, since pkl never emits into it in the first place.

    Returns False — touching nothing under ``generated_dir`` — when the output
    holds no generated artifacts (``root-only``/``empty``) or when the native
    target looks implausible. Callers treat False as "regeneration did not
    happen" and must surface it; a silent False is the bug this module exists to
    remove. Note that root files may still have been copied on a False return:
    they are unambiguous and correct in every layout.
    """
    layout, plan = plan_swap(out_dir, generated_dir)
    target = Path(generated_dir)

    if layout not in ("prefixed", "native"):
        print(
            f"::warning::pkl eval produced no generated contract artifacts "
            f"({layout}) — {generated_dir}/ left unchanged."
        )
        _copy_planned(plan, skip=set())
        return False

    if layout == "native":
        entries = sorted(
            {
                dest.relative_to(target).parts[0]
                for dest in plan
                if dest.is_relative_to(target)
            }
        )
        if not _native_target_is_plausible(entries, target):
            print(
                f"::warning::pkl eval emitted unprefixed contract artifacts "
                f"({', '.join(entries)}) but none of them match anything in "
                f"{generated_dir}/ — this app most likely generates into a "
                f"different directory. Refusing to write to the wrong base; "
                f"generated artifacts left unchanged. Regenerate with the app's "
                f"own generate task (e.g. `uv run poe generate`) and commit the "
                f"result."
            )
            return False

    skip: set[Path] = set()
    if baseline_dir is not None:
        _, baseline_plan = plan_swap(baseline_dir, generated_dir)
        skip = overridden_files(plan, baseline_plan)
        if skip:
            print(
                "::notice::Preserved app-maintained generated file(s) — committed "
                "content differs from what the previous toolkit pin emitted, so "
                "the app post-processes them: "
                + ", ".join(sorted(str(p) for p in skip))
                + ". If any of those is NOT app-maintained, this app's committed "
                "artifacts are stale against its own contract — regenerate with "
                "the app's generate task and commit."
            )

    if layout == "prefixed":
        # App.pkl owns every file under the generated dir, so replacing it
        # wholesale is safe and clears orphans. Preserved files are read out
        # first and written back after, so the wholesale replace keeps them.
        preserved = {
            dest: dest.read_bytes() for dest in skip if dest.is_relative_to(target)
        }
        reserved = _withhold_reserved_subdirs(target)
        try:
            shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(out_dir / "app" / "generated", target)
            _copy_planned(
                {d: s for d, s in plan.items() if not d.is_relative_to(target)}, skip
            )
            for dest, content in preserved.items():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
        finally:
            # Restore even on a mid-swap failure: the withheld reserved subdirs
            # live in a tempdir, so an exception here would otherwise strand the
            # working tree without them. The swap failing still propagates; this
            # just guarantees the restore runs.
            _restore_reserved_subdirs(target, reserved)
        return True

    # Native: overwrite-only. The generated dir can hold app-owned files this
    # eval does not emit, and an unprefixed key set cannot tell them from
    # orphans. See the module docstring.
    _copy_planned(plan, skip)
    return True


def _copy_planned(plan: dict[Path, Path], skip: set[Path]) -> None:
    """Copy each planned (dest, src) pair, minus the preserved destinations."""
    for dest, src in sorted(plan.items()):
        if dest in skip:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def baseline_contract_ref(contract_dir: str = "contract") -> str | None:
    """Git rev holding the contract state the committed artifacts were generated
    from, or None when there is no pin change in flight (or git can't tell us).

    "Baseline" means: the last state where ``PklProject`` — hence the toolkit pin
    — differed from what the tree has now. Two shapes, because the two ways a
    bump reaches this code differ in whether it is committed yet:

      * Renovate ``postUpgradeTasks`` (``--no-commit``): Renovate rewrote
        ``contract/PklProject`` in the working tree but has not committed, so
        ``HEAD`` still holds the old pin.
      * An already-committed bump — a human editing the pin, or the freshness
        gate running on a PR head. The baseline is then the parent of the last
        commit that touched ``PklProject``. (Until FND-395 the per-app
        ``renovate-pkl-sync.yaml`` shim was the main way to arrive here; it is
        retired, but a committed bump still reaches this code by both routes
        above, so the branch stays.)

    Returns None when the pin is unchanged — which is the common case on an
    ordinary PR, and deliberately switches override detection OFF there. With no
    bump in flight the "baseline" would be an arbitrarily old toolkit version, and
    every artifact the toolkit has changed since would read as an app override —
    freezing the tree and making the freshness gate blind again. Override
    protection only makes sense while a pin change is what's in flight.

    Also returns None when git is unavailable or history is too shallow to name
    the parent commit (a depth-1 checkout). Callers then overwrite as before and
    say so, because preserving everything would regenerate nothing.
    """
    pkl_project = f"{contract_dir}/PklProject"
    if not Path(pkl_project).exists():
        return None

    # Establish a usable HEAD first. Without it every check below is meaningless,
    # and `git diff` reports a fatal error with an exit code that is easy to
    # mistake for "differs" (it is not always 128 — this cost a test).
    if _git("rev-parse", "--verify", "HEAD").returncode != 0:
        return None  # not a repo, or no commits yet.

    # Uncommitted bump (postUpgradeTasks): HEAD still has the old pin.
    diff = _git("diff", "--quiet", "HEAD", "--", pkl_project)
    if diff.returncode == 1:
        return "HEAD"
    if diff.returncode != 0:
        return None  # anything unexpected: no baseline rather than a wrong one.

    # Committed bump (workflow shim): parent of the last commit touching it.
    last = _git("log", "-1", "--format=%H", "--", pkl_project)
    if last.returncode != 0 or not last.stdout.strip():
        return None
    parent = _git("rev-parse", "--verify", f"{last.stdout.strip()}^")
    if parent.returncode != 0:
        return None  # root commit, or a shallow clone that lacks the parent.
    return parent.stdout.strip()


def export_contract_at(ref: str, contract_dir: str, dest: Path) -> bool:
    """Materialise ``contract_dir`` as of ``ref`` under ``dest``. False on failure.

    ``git archive`` keeps this to one plumbing call and cannot touch the working
    tree. The exported tree carries that revision's ``PklProject.deps.json``, so
    the baseline eval resolves the OLD toolkit package without re-resolving.
    """
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", ref, contract_dir],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        return False
    extract = subprocess.run(
        ["tar", "-x", "-C", str(dest)], input=archive.stdout, check=False
    )
    if extract.returncode != 0:
        return False
    # Validity is "the archive holds an evaluable contract", NOT "it holds
    # app.pkl": an app with one root per entrypoint (crawler.pkl + miner.pkl)
    # has no app.pkl and would otherwise never get a baseline, silently
    # turning override detection off for exactly the apps whose generated
    # trees are most likely to be post-processed.
    exported = dest / contract_dir
    if (exported / "app.pkl").exists():
        return True
    return any(
        any(
            ln.startswith("amends ")
            for ln in f.read_text(encoding="utf-8").splitlines()
        )
        for f in sorted(exported.glob("*.pkl"))
        if f.is_file()
    )


def run_post_generate(contract_dir: str = "contract") -> None:
    """Run ``<contract_dir>/post-generate.sh`` if the app ships one, after
    ``swap_outputs``. No-op otherwise, which is almost every app.

    Placement is layout-aware but content is not app-aware: some apps install a
    hand-maintained artifact over the toolkit's output for a construct the
    toolkit cannot yet express (a semicolon-delimited JDBC URL group, conditional
    file-upload widgets keyed on an auth mode). Without this step a working swap
    reverts that override and ships the unusable version — so the app owns the
    step, and every regeneration entry point runs it from the same conventional
    path. See ``POST_GENERATE_SCRIPT`` for why this is a convention and not an
    input.

    The script runs from the repo root (cwd) with ``sh``, so it needs no
    executable bit — one less thing for an app to get wrong. It is app-repo
    content executed in the app's own CI, the same trust level as the
    ``contract/app.pkl`` this module just evaluated and the app's own test suite.

    Best-effort by design: this runs after the swap, so a failure leaves fresh
    toolkit output in the tree rather than blocking the caller. The resulting
    diff is visible for a human to judge, which beats failing a dependency bump
    over an app-side script.

    SECURITY: the safety of running app-repo content rests entirely on the
    caller using a ``push``-triggered (same-repo-branch) workflow, where placing
    the script already requires push access to that repo. This must never be
    wired into a ``pull_request`` / ``pull_request_target`` context, where a
    fork's branch content runs with a token — there the same line is a genuine
    untrusted-code-execution path."""
    script = Path(contract_dir) / POST_GENERATE_SCRIPT
    if not script.is_file():
        return
    print(f"Running post-generate step: {script}")
    if subprocess.run(["sh", str(script)], text=True).returncode != 0:
        print(
            f"::warning::{script} failed — generated artifacts are raw `pkl eval` "
            "output, so anything this step installs over them is missing. Review "
            "the diff before merging."
        )
