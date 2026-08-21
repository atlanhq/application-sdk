"""The scaffolded ``tests.yaml`` must declare the token the reusable needs (FND-702).

A called workflow's ``permissions`` can only EQUAL or NARROW its caller's, so a
caller with no block at all satisfies ``tests-reusable.yaml``'s ``contents:
write`` purely from ``default_workflow_permissions`` being ``write``. Tightening
that repository setting — an ordinary hardening step, and one that could
plausibly be applied org-wide — would then strip the tenant lease's ref-write
grant in every adopted connector at once.

Two things are pinned here, and the second is the one that keeps working after
this change is forgotten:

* the block exists and carries the lease's own two grants, by name; and
* its scope set is derived from ``tests-reusable.yaml`` rather than restated, so
  a new scope added to any job of the reusable fails HERE, at the one place a
  caller could otherwise silently clamp it to ``none``.

That second property is why a caller's block is dangerous to add carelessly: a
``permissions:`` block is exhaustive, not additive. Declaring
``permissions: {contents: read}`` on the caller is strictly worse than declaring
nothing, because it clamps the lease job whose default would have carried it.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from conformance.bootstrap.extract import (
    declared_keys,
    unpreserved_tests_yaml_declarations,
)
from conformance.bootstrap.render import render

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REUSABLE = _REPO_ROOT / ".github/workflows/tests-reusable.yaml"

#: GitHub's permission levels, weakest first. Used to take the maximum a scope is
#: declared at across the reusable's jobs — a caller has ONE block covering every
#: job it calls, so it must carry each scope at the strongest level any job uses.
_LEVELS = ("none", "read", "write")


def _caller_job() -> dict:  # type: ignore[type-arg]
    """The `tests:` job of the canonical scaffolded caller, as GitHub reads it."""
    rendered = yaml.safe_load(render("tests.yaml", app_name="example"))
    return rendered["jobs"]["tests"]


def _required_scopes() -> dict[str, str]:
    """Every scope the reusable's jobs declare, at the strongest level used.

    Read from the workflow rather than listed, so this file cannot fall out of
    date with it: adding `checks: write` to one job of the reusable makes the
    assertions below fail until the scaffolded caller carries it too.
    """
    workflow = yaml.safe_load(_REUSABLE.read_text(encoding="utf-8"))
    required: dict[str, str] = {}
    for job in workflow["jobs"].values():
        for scope, level in (job.get("permissions") or {}).items():
            if _LEVELS.index(level) > _LEVELS.index(required.get(scope, "none")):
                required[scope] = level
    return required


def test_the_scaffolded_caller_declares_a_permissions_block() -> None:
    assert "permissions" in _caller_job(), (
        "without a block the whole fleet's e2e install depends on "
        "default_workflow_permissions being `write` (FND-702)"
    )


def test_the_scaffolded_caller_grants_the_tenant_lease_its_two_permissions() -> None:
    # Named explicitly as well as derived below, because these two are the ones
    # whose absence produces the FND-702 failure: the acquire is denied, and
    # every `Prepare tenant` leg then reds on a lease that was never taken.
    permissions = _caller_job()["permissions"]
    assert permissions["contents"] == "write"  # create/delete the lease ref
    assert permissions["actions"] == "read"  # tell a live holder from a dead one


def test_the_scaffolded_caller_declares_every_scope_the_reusable_uses() -> None:
    # The wiring assertion. A caller's block is exhaustive rather than additive,
    # so any scope missing here is `none` for every job of the reusable —
    # regardless of what that job declares for itself.
    permissions = _caller_job()["permissions"]
    for scope, level in _required_scopes().items():
        assert scope in permissions, (
            f"tests-reusable.yaml declares `{scope}: {level}` on one of its jobs, "
            f"but the scaffolded caller omits it — which clamps it to `none`. Add "
            f"it to conformance/bootstrap/templates/tests.yaml."
        )
        assert _LEVELS.index(permissions[scope]) >= _LEVELS.index(level), (
            f"the scaffolded caller declares `{scope}: {permissions[scope]}` but "
            f"the reusable needs `{level}`; a caller can only narrow."
        )


def test_the_scaffolded_caller_grants_nothing_the_reusable_does_not_use() -> None:
    # Least privilege in the other direction: the block is a ceiling for every
    # job of the reusable, so an extra scope is a standing grant to code that
    # never asked for it. Failing here means either delete the line, or the job
    # that needs it belongs in the reusable where it is reviewable.
    extra = set(_caller_job()["permissions"]) - set(_required_scopes())
    assert not extra, f"the scaffolded caller grants unused scopes: {sorted(extra)}"


# ── The rollout has to survive --resync (FND-604) ────────────────────────────


def _existing_without_permissions() -> str:
    """A repo's tests.yaml as it stands today: the canonical, block removed."""
    rendered = render("tests.yaml", app_name="example")
    lines = rendered.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.strip() == "permissions:")
    end = next(
        i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("uses:")
    )
    stripped = "".join(lines[:start] + lines[end:])
    # By the same structural reading `unpreserved_declarations` uses, so the
    # surrounding comment block — which says the word — cannot make this vacuous.
    assert "permissions" not in declared_keys(stripped)
    return stripped


def test_resync_onto_a_caller_that_predates_the_block_drops_nothing() -> None:
    # FND-604 makes --resync REFUSE when the re-render would delete a declaration
    # the file carries. Adding a block only ever adds keys, so every already-
    # adopted repo can take this update with a plain `bootstrap --resync`.
    dropped = unpreserved_tests_yaml_declarations(
        _existing_without_permissions(), render("tests.yaml", app_name="example")
    )
    assert dropped == []


def test_resync_onto_a_caller_that_hand_rolled_its_own_block_drops_nothing() -> None:
    # The dangerous edit this change exists to pre-empt — a well-meaning
    # `permissions: contents: read` — is a key present on BOTH sides, so the
    # resync proceeds and overwrites it with the correct set rather than
    # refusing and leaving the clamp in place.
    hand_rolled = _existing_without_permissions().replace(
        "  tests:\n",
        "  tests:\n    permissions:\n      contents: read\n",
        1,
    )
    dropped = unpreserved_tests_yaml_declarations(
        hand_rolled, render("tests.yaml", app_name="example")
    )
    assert dropped == []
