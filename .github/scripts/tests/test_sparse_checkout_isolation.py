"""Cross-file guard: a sparse checkout must not share a path with a full one.

`actions/checkout` with `sparse-checkout:` writes `core.sparseCheckout=true` into
`.git/config` at **local** scope, plus the narrow pattern into
`.git/info/sparse-checkout`. A later checkout into the same path then runs, in
this order:

1. `git sparse-checkout disable` — parks `core.sparseCheckout=false` in the
   *worktree* config (`.git/config.worktree`) and sets `extensions.worktreeConfig`.
   Worktree config shadows local, so the tree is restored at this instant.
2. `git config --local --unset-all extensions.worktreeConfig` — makes the
   worktree config inert. `core.sparseCheckout` reverts to **true** from the
   local config, and the narrow pattern file is still on disk.
3. `git checkout --force -B <ref>` — re-applies the sparse pattern.

Net effect: the workspace stays sparse for the rest of the job. The failure is
silent and far from its cause — in FND-637 it surfaced as
`error: Failed to spawn: poe`, because `uv run` found no `pyproject.toml` and
fell back to spawning a bare binary. Nothing in the checkout logs says the tree
is narrow; `git sparse-checkout disable` even appears to have run.

The fix is always the same: give the sparse checkout its own `path:`, so it
never shares a git dir with the full checkout. This guard asserts that property
structurally rather than trusting reviewers to remember the interaction.

Scoping is per (file, job, path). Two sparse checkouts at the same path are fine
— the trap needs a *full* checkout to land on a path a sparse one already
claimed. Different jobs get different runners, so they cannot collide.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHECKOUT_ACTION = "actions/checkout@"

# `path:` defaults to the workspace root when omitted.
DEFAULT_PATH = "."


def _yaml_files() -> list[Path]:
    files = sorted((ROOT / "workflows").glob("*.y*ml"))
    files += sorted((ROOT / "actions").glob("*/action.y*ml"))
    return files


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _normalise_path(raw: object) -> str:
    """Collapse the spellings that name the same directory."""
    text = str(raw or DEFAULT_PATH).strip()
    if not text:
        return DEFAULT_PATH
    return str(Path(text).as_posix()).rstrip("/") or DEFAULT_PATH


def _scopes() -> list[tuple[str, str, list[dict]]]:
    """(file, scope, steps) per job and per composite `runs` block."""
    scopes: list[tuple[str, str, list[dict]]] = []
    for path in _yaml_files():
        rel = _rel(path)
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            pytest.fail(f"{rel} is not valid YAML ({exc.__class__.__name__}): {exc}")
        if not isinstance(doc, dict):
            continue
        for job_id, job in (doc.get("jobs") or {}).items():
            if isinstance(job, dict):
                steps = [s for s in (job.get("steps") or []) if isinstance(s, dict)]
                if steps:
                    scopes.append((rel, f"job {job_id}", steps))
        runs = doc.get("runs") or {}
        if isinstance(runs, dict):
            steps = [s for s in (runs.get("steps") or []) if isinstance(s, dict)]
            if steps:
                scopes.append((rel, "composite runs", steps))
    return scopes


def _label(step: dict, index: int) -> str:
    return str(step.get("name") or step.get("id") or f"step #{index}")


def collisions(steps: list[dict]) -> list[tuple[str, str, str]]:
    """(path, sparse step, full step) for every sparse-then-full pair."""
    claimed: dict[str, str] = {}
    found: list[tuple[str, str, str]] = []
    for index, step in enumerate(steps):
        if CHECKOUT_ACTION not in str(step.get("uses") or ""):
            continue
        with_block = step.get("with") or {}
        if not isinstance(with_block, dict):
            with_block = {}
        path = _normalise_path(with_block.get("path"))
        if "sparse-checkout" in with_block:
            claimed.setdefault(path, _label(step, index))
        elif path in claimed:
            found.append((path, claimed[path], _label(step, index)))
    return found


def test_no_full_checkout_reuses_a_sparse_checkout_path():
    offenders = []
    for rel, scope, steps in _scopes():
        for path, sparse_step, full_step in collisions(steps):
            offenders.append(
                f"{rel} ({scope}): '{sparse_step}' checks out sparsely at "
                f"'{path}', then '{full_step}' does a full checkout at the same "
                f"path — the tree stays sparse. Give the sparse step its own "
                f"`path:`."
            )
    assert not offenders, "\n".join(offenders)


def test_guard_detects_the_fnd637_shape():
    """The bug as it actually shipped: sparse ack, then full checkout at root."""
    steps = [
        {
            "name": "Checkout reaction helper",
            "uses": "actions/checkout@abc",
            "with": {
                "sparse-checkout": ".github/scripts/react_to_comment.py",
                "sparse-checkout-cone-mode": False,
            },
        },
        {"name": "Checkout PR branch", "uses": "actions/checkout@abc"},
    ]
    assert collisions(steps) == [
        (".", "Checkout reaction helper", "Checkout PR branch")
    ]


def test_separate_paths_are_allowed():
    steps = [
        {
            "name": "Checkout reaction helper",
            "uses": "actions/checkout@abc",
            "with": {"path": ".ack", "sparse-checkout": ".github/scripts/x.py"},
        },
        {"name": "Checkout PR branch", "uses": "actions/checkout@abc"},
    ]
    assert collisions(steps) == []


def test_full_checkout_before_the_sparse_one_is_allowed():
    """Order matters: only a full checkout *after* a sparse one inherits it."""
    steps = [
        {"name": "Checkout PR branch", "uses": "actions/checkout@abc"},
        {
            "name": "Checkout helper",
            "uses": "actions/checkout@abc",
            "with": {"sparse-checkout": ".github/scripts/x.py"},
        },
    ]
    assert collisions(steps) == []


def test_two_sparse_checkouts_at_one_path_are_allowed():
    steps = [
        {
            "name": "First",
            "uses": "actions/checkout@abc",
            "with": {"sparse-checkout": "a.py"},
        },
        {
            "name": "Second",
            "uses": "actions/checkout@abc",
            "with": {"sparse-checkout": "b.py"},
        },
    ]
    assert collisions(steps) == []


@pytest.mark.parametrize("spelling", [None, "", ".", "./", "./."])
def test_root_path_spellings_collapse_to_one_path(spelling):
    """`path: ./` and an omitted `path:` name the same directory."""
    sparse_with: dict[str, object] = {"sparse-checkout": "a.py"}
    if spelling is not None:
        sparse_with["path"] = spelling
    steps = [
        {"name": "Sparse", "uses": "actions/checkout@abc", "with": sparse_with},
        {"name": "Full", "uses": "actions/checkout@abc"},
    ]
    assert collisions(steps) == [(".", "Sparse", "Full")]


def test_non_checkout_steps_are_ignored():
    steps = [
        {
            "name": "Sparse",
            "uses": "actions/checkout@abc",
            "with": {"path": ".ack", "sparse-checkout": "a.py"},
        },
        {
            "name": "Setup uv",
            "uses": "astral-sh/setup-uv@abc",
            "with": {"path": ".ack"},
        },
    ]
    assert collisions(steps) == []
