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
    entrypoint.

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
from pathlib import Path

# Repo-root files both families emit at the top level of the eval output. They
# live at the repo root, never inside the generated dir.
ROOT_FILES = ("atlan.yaml", "app.yaml")

# Where an app keeps its generated artifacts. The overwhelming default across
# the fleet; apps that differ are refused rather than mis-written.
GENERATED_DIR = "app/generated"


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


def _native_target_is_plausible(sources: list[Path], target: Path) -> bool:
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
    return any(src.name in existing for src in sources)


def swap_outputs(out_dir: Path, generated_dir: str = GENERATED_DIR) -> bool:
    """Move eval output from ``out_dir`` into the working tree. Returns True iff
    generated artifacts were actually placed.

    Repo-root files (``atlan.yaml``/``app.yaml``) are copied for either family
    whenever emitted. The generated artifacts are placed per the family rules in
    this module's docstring.

    Returns False — touching nothing under ``generated_dir`` — when the output
    holds no generated artifacts (``root-only``/``empty``) or when the native
    target looks implausible. Callers treat False as "regeneration did not
    happen" and must surface it; a silent False is the bug this module exists to
    remove. Note that root files may still have been copied on a False return:
    they are unambiguous and correct in every layout.
    """
    layout = detect_layout(out_dir)

    for name in ROOT_FILES:
        src = out_dir / name
        if src.exists():
            shutil.copyfile(src, name)

    target = Path(generated_dir)

    if layout == "prefixed":
        # App.pkl owns every file under the generated dir, so replacing it
        # wholesale is safe and clears orphans.
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(out_dir / "app" / "generated", target)
        return True

    if layout == "native":
        sources = [p for p in sorted(out_dir.iterdir()) if p.name not in ROOT_FILES]
        if not _native_target_is_plausible(sources, target):
            print(
                f"::warning::pkl eval emitted unprefixed contract artifacts "
                f"({', '.join(p.name for p in sources)}) but none of them match "
                f"anything in {generated_dir}/ — this app most likely generates "
                f"into a different directory. Refusing to write to the wrong "
                f"base; generated artifacts left unchanged. Regenerate with the "
                f"app's own generate task (e.g. `uv run poe generate`) and "
                f"commit the result."
            )
            return False
        # Overwrite-only: the generated dir can hold app-owned files this eval
        # does not emit, and an unprefixed key set cannot tell them from
        # orphans. See the module docstring.
        target.mkdir(parents=True, exist_ok=True)
        for src in sources:
            dest = target / src.name
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copyfile(src, dest)
        return True

    print(
        f"::warning::pkl eval produced no generated contract artifacts "
        f"({layout}) — {generated_dir}/ left unchanged."
    )
    return False


def run_post_generate(command: str) -> None:
    """Run an app's declared post-generate step, after ``swap_outputs``.

    Placement is layout-aware but content is not app-aware: some apps install a
    hand-maintained artifact over the toolkit's output for a construct the
    toolkit cannot yet express (a semicolon-delimited JDBC URL group, conditional
    file-upload widgets). Without this hook the swap reverts that override and
    ships the unusable version — so the app declares the step and every
    regeneration entry point runs it, or they disagree about what "fresh" means.

    Best-effort by design: this runs after the swap, so a failure leaves fresh
    toolkit output in the tree rather than blocking the caller. The resulting
    diff is visible for a human to judge, which beats failing a dependency bump
    over an app-side script."""
    if not command:
        return
    print(f"Running post-generate hook: {command}")
    if subprocess.run(command, shell=True, text=True).returncode != 0:
        print(
            "::warning::post-generate hook failed — generated artifacts are raw "
            "`pkl eval` output. Review the diff before merging."
        )
