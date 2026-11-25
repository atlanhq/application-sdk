"""Guards for the seams between tests-reusable.yaml and the tiers it calls.

FND-4676 split tests-reusable.yaml into an entry point plus three tier
workflows (unit, integration, e2e). Each seam is a place GitHub fails quietly:

* a caller job's ``permissions:`` is the ceiling for every job it calls, so a
  scope a tier job declares but its caller omits arrives as ``none``;
* a tier input the caller does not forward fails at parse time, but one it
  forwards from the wrong input is a silent rewire;
* a tier's outputs are empty when its caller job is skipped or cancelled, so
  the gate must fall back to the caller's own result or a cancelled tier would
  read as no verdict at all.

Asserted as YAML shape, like the other wiring tests: the behaviour belongs to
GitHub Actions and cannot be exercised without a runner.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from _workflows import (
    TESTS_REUSABLE,
    TIER_WORKFLOWS,
    WORKFLOWS,
    load_workflow,
    triggers,
)

_LEVELS = ("none", "read", "write")

#: tests-reusable.yaml's caller job for each tier.
CALLERS = {
    "unit": "unit-tests-reusable.yaml",
    "integration": "integration-tests-reusable.yaml",
    "e2e": "e2e-tests-reusable.yaml",
}

#: Inputs a tier takes that are not tests-reusable.yaml inputs: discovery's
#: outputs, handed to the e2e tier.
_DISCOVERY_INPUTS = {
    "e2e-matrix": "${{ needs.discover-e2e.outputs.matrix }}",
    "e2e-cloud-matrix": "${{ needs.discover-e2e.outputs.cloud-matrix }}",
    "e2e-cloud-count": "${{ needs.discover-e2e.outputs.cloud-count }}",
    "e2e-cloud-list": "${{ needs.discover-e2e.outputs.cloud-list }}",
}


def _entry() -> dict[str, Any]:
    return load_workflow(TESTS_REUSABLE)


def _call(tier: str) -> dict[str, Any]:
    return triggers(load_workflow(tier))["workflow_call"]


def test_the_callers_cover_every_tier() -> None:
    assert sorted(CALLERS.values()) == sorted(TIER_WORKFLOWS)


@pytest.mark.parametrize(("caller", "tier"), CALLERS.items())
def test_each_tier_is_called_at_main_with_inherited_secrets(
    caller: str, tier: str
) -> None:
    job = _entry()["jobs"][caller]
    assert job["uses"] == f"atlanhq/application-sdk/.github/workflows/{tier}@main"
    assert job["secrets"] == "inherit"


@pytest.mark.parametrize(("caller", "tier"), CALLERS.items())
def test_the_caller_grants_every_scope_the_tier_uses(caller: str, tier: str) -> None:
    granted: dict[str, str] = _entry()["jobs"][caller].get("permissions") or {}
    for name, job in load_workflow(tier)["jobs"].items():
        for scope, level in (job.get("permissions") or {}).items():
            have = granted.get(scope, "none")
            assert _LEVELS.index(have) >= _LEVELS.index(level), (
                f"{tier} job {name} declares `{scope}: {level}` but the "
                f"`{caller}` caller grants `{have}`; a called job can only narrow"
            )


@pytest.mark.parametrize(("caller", "tier"), CALLERS.items())
def test_the_caller_grants_nothing_the_tier_does_not_use(
    caller: str, tier: str
) -> None:
    granted: dict[str, str] = _entry()["jobs"][caller].get("permissions") or {}
    used: dict[str, str] = {}
    for job in load_workflow(tier)["jobs"].values():
        for scope, level in (job.get("permissions") or {}).items():
            if _LEVELS.index(level) > _LEVELS.index(used.get(scope, "none")):
                used[scope] = level
    assert granted == used


@pytest.mark.parametrize(("caller", "tier"), CALLERS.items())
def test_every_tier_input_is_forwarded_from_its_namesake(
    caller: str, tier: str
) -> None:
    forwarded = _entry()["jobs"][caller]["with"]
    declared = _call(tier)["inputs"]
    entry_inputs = triggers(_entry())["workflow_call"]["inputs"]
    assert set(forwarded) == set(declared), tier
    for name, spec in declared.items():
        if name in _DISCOVERY_INPUTS:
            assert forwarded[name] == _DISCOVERY_INPUTS[name], name
            continue
        assert forwarded[name] == f"${{{{ inputs.{name} }}}}", name
        assert spec["type"] == entry_inputs[name]["type"], name


@pytest.mark.parametrize("tier", TIER_WORKFLOWS)
def test_tier_inputs_carry_no_second_default(tier: str) -> None:
    """The defaults live in tests-reusable.yaml. A second copy here would be
    dead while it matched and a silent divergence once it did not; the one
    exception is the literal test_job_timeouts.py reads."""
    entry_inputs = triggers(_entry())["workflow_call"]["inputs"]
    for name, spec in _call(tier)["inputs"].items():
        if name == "timeout-minutes":
            assert spec["default"] == entry_inputs[name]["default"]
            continue
        assert spec["required"] is True and "default" not in spec, (tier, name)


@pytest.mark.parametrize("tier", TIER_WORKFLOWS)
def test_tier_secrets_are_declared_by_the_entry_point(tier: str) -> None:
    """A tier can only see what tests-reusable.yaml's caller passes on."""
    entry_secrets = triggers(_entry())["workflow_call"]["secrets"]
    assert set(_call(tier).get("secrets") or {}) <= set(entry_secrets)


_TIER_OUTPUT = re.compile(r"needs\.(unit|e2e)\.outputs\.([a-z0-9-]+)")


def test_every_tier_output_the_entry_point_reads_exists() -> None:
    text = (WORKFLOWS / TESTS_REUSABLE).read_text(encoding="utf-8")
    for match in _TIER_OUTPUT.finditer(text):
        caller, output = match.group(1), match.group(2)
        assert output in _call(CALLERS[caller])["outputs"], (caller, output)


_RESULT_OUTPUT = re.compile(
    r"needs\.(unit|e2e)\.outputs\.([a-z0-9-]+-result)\b(?P<rest>[^\n]*)"
)


def test_a_tier_result_falls_back_to_the_callers_own() -> None:
    """Empty outputs mean the tier never wrote them: its caller was skipped,
    cancelled, or failed at startup. Falling back to the caller's result keeps
    that a skip or a failure, never a pass. The one exception is the e2e verdict
    the gate records, where no verdict is already `error`."""
    text = (WORKFLOWS / TESTS_REUSABLE).read_text(encoding="utf-8")
    for match in _RESULT_OUTPUT.finditer(text):
        caller, output, rest = match.group(1), match.group(2), match.group("rest")
        if rest.lstrip().startswith(
            ("== 'success'", "== 'failure'", "&&", ")", "|| 'error'")
        ):
            # The e2e verdict expressions: compare, then map anything else to
            # `error`. Checked by test_e2e_label_consumption.py.
            assert output == "e2e-result", match.group(0)
            continue
        assert rest.lstrip().startswith(f"|| needs.{caller}.result"), match.group(0)


@pytest.mark.parametrize("tier", TIER_WORKFLOWS)
def test_tier_jobs_reach_no_entry_point_job(tier: str) -> None:
    """A `needs.<job>` naming a tests-reusable.yaml job cannot resolve inside a
    called workflow; it renders empty, which reads as "not superseded", "no
    suites" and the like."""
    entry_only = set(_entry()["jobs"]) - set(load_workflow(tier)["jobs"])
    text = (WORKFLOWS / tier).read_text(encoding="utf-8")
    for name in entry_only:
        assert f"needs.{name}." not in text, (tier, name)
