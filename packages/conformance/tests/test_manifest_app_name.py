"""Tests for K013 ManifestNodeAppNameMismatch / K014 ManifestNodeAppNameCollision.

Both rules read the committed generated DAG directly (``app/generated/`` or
``contract/generated/``, no Python source), so the tests only need to write
manifest JSON under a temp repo root and call :func:`scan_all`.

Helpers
-------
``_node``: build one DAG node with configurable ``app_name`` in the three
places it can appear (top-level, ``inputs.app_name``, ``inputs.args.app_name``);
pass ``None`` to omit a given slot.
``_run``: write ``{relative_manifest_path: dag_dict}`` under
``tmp_path/app/generated/`` and return :func:`scan_all`'s findings.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.manifest_app_name import scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(
    top: str | None = None,
    inputs_app: str | None = None,
    args_app: str | None = None,
    workflow_type: str = "MyWorkflow",
) -> dict:
    node: dict = {"activity_name": "execute_workflow"}
    if top is not None:
        node["app_name"] = top
    inputs: dict = {"workflow_type": workflow_type}
    if inputs_app is not None:
        inputs["app_name"] = inputs_app
    args: dict = {"credential": "{{credential}}"}
    if args_app is not None:
        args["app_name"] = args_app
    inputs["args"] = args
    node["inputs"] = inputs
    return node


def _run(
    tmp_path: Path,
    manifests: dict[str, dict],
    base: str = "app/generated",
) -> list:
    """Write manifests under ``tmp_path/<base>/`` and return scan_all's findings.

    ``base`` selects the generated root — ``app/generated`` (default, most apps)
    or ``contract/generated`` (powerbi and friends).
    """
    generated = tmp_path / base
    for rel, dag in manifests.items():
        p = generated / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"dag": dag}), encoding="utf-8")
    return scan_all([], tmp_path)


def _ids(findings: list, rule_id: str) -> list:
    return [f for f in findings if f.rule_id == rule_id]


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_k013_metadata() -> None:
    rule = get_rule("K013")
    assert rule.name == "ManifestNodeAppNameMismatch"
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.scope == RuleScope.APP


def test_k014_metadata() -> None:
    rule = get_rule("K014")
    assert rule.name == "ManifestNodeAppNameCollision"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP


# ---------------------------------------------------------------------------
# (a) consistent + distinct → both pass
# ---------------------------------------------------------------------------


def test_consistent_and_distinct_is_clean(tmp_path: Path) -> None:
    dag = {
        "extract": _node("powerbi-crawler", "powerbi-crawler", "powerbi-crawler"),
        "publish": _node("publish", "publish", "publish"),
    }
    assert _run(tmp_path, {"manifest.json": dag}) == []


# ---------------------------------------------------------------------------
# (b) inputs.args.app_name != top-level → K013 BLOCK
# ---------------------------------------------------------------------------


def test_args_app_name_mismatch_flags_k013(tmp_path: Path) -> None:
    # SDK would stamp "powerbi" while the UI queries "powerbi-crawler".
    dag = {"extract": _node("powerbi-crawler", "powerbi-crawler", "powerbi")}
    findings = _run(tmp_path, {"manifest.json": dag})
    k013 = _ids(findings, "K013")
    assert len(k013) == 1
    assert "extract" in k013[0].message
    assert "powerbi-crawler" in k013[0].message and "powerbi" in k013[0].message
    assert _ids(findings, "K014") == []


def test_inputs_app_name_mismatch_flags_k013(tmp_path: Path) -> None:
    # args present and agrees with top, but inputs.app_name diverges → still flagged.
    dag = {"extract": _node("crawler", "stale", "crawler")}
    assert len(_ids(_run(tmp_path, {"manifest.json": dag}), "K013")) == 1


# ---------------------------------------------------------------------------
# (c) two distinct nodes sharing an app_name in one manifest → K014 WARN
# ---------------------------------------------------------------------------


def test_shared_app_name_across_nodes_flags_k014(tmp_path: Path) -> None:
    # Each node is internally consistent (no K013), but they collide (K014).
    dag = {
        "extract": _node("dup", "dup", "dup"),
        "notify": _node("dup", "dup", "dup"),
    }
    findings = _run(tmp_path, {"manifest.json": dag})
    k014 = _ids(findings, "K014")
    assert len(k014) == 1
    assert "extract" in k014[0].message and "notify" in k014[0].message
    assert "dup" in k014[0].message
    assert _ids(findings, "K013") == []


# ---------------------------------------------------------------------------
# (d) inputs.args.app_name absent everywhere → K013 no-op
# ---------------------------------------------------------------------------


def test_absent_args_app_name_is_k013_noop(tmp_path: Path) -> None:
    # top and inputs disagree, but NO args.app_name → not-yet-regenerated app,
    # falls back to env at runtime; must not be flagged.
    dag = {"extract": _node("powerbi-crawler", "something-else", None)}
    findings = _run(tmp_path, {"manifest.json": dag})
    assert _ids(findings, "K013") == []


def test_no_app_name_at_all_is_clean(tmp_path: Path) -> None:
    dag = {"extract": _node(None, None, None)}
    assert _run(tmp_path, {"manifest.json": dag}) == []


# ---------------------------------------------------------------------------
# (e) same app_name across SEPARATE manifests → K014 does NOT fire
# ---------------------------------------------------------------------------


def test_collision_is_scoped_per_manifest(tmp_path: Path) -> None:
    crawler = {
        "extract": _node("powerbi-crawler", "powerbi-crawler", "powerbi-crawler"),
        "publish": _node("publish", "publish", "publish"),
    }
    miner = {
        "extract": _node("powerbi-miner", "powerbi-miner", "powerbi-miner"),
        "publish": _node("publish", "publish", "publish"),
    }
    findings = _run(
        tmp_path,
        {"crawler/manifest.json": crawler, "miner/manifest.json": miner},
    )
    # 'publish' recurs across the two entrypoint manifests — separate runs, no overlap.
    assert _ids(findings, "K014") == []
    assert _ids(findings, "K013") == []


# ---------------------------------------------------------------------------
# No-op / robustness
# ---------------------------------------------------------------------------


def test_noop_when_neither_generated_dir(tmp_path: Path) -> None:
    # Neither app/generated/ nor contract/generated/ exists (SDK / library repo).
    assert scan_all([], tmp_path) == []


# ---------------------------------------------------------------------------
# contract/generated/ layout (powerbi and friends) — both rules fire there too
# ---------------------------------------------------------------------------


def test_contract_generated_layout_flags_k013(tmp_path: Path) -> None:
    # powerbi generates to contract/generated/<entrypoint>/manifest.json.
    crawler = {
        # SDK would stamp "powerbi" while the UI queries "powerbi-crawler".
        "extract": _node("powerbi-crawler", "powerbi-crawler", "powerbi"),
        "publish": _node("publish", "publish", "publish"),
    }
    findings = _run(
        tmp_path, {"crawler/manifest.json": crawler}, base="contract/generated"
    )
    k013 = _ids(findings, "K013")
    assert len(k013) == 1
    assert "extract" in k013[0].message
    # relative-path reporting anchored at repo root, from the contract/ root.
    assert k013[0].file == "contract/generated/crawler/manifest.json"


def test_contract_generated_layout_flags_k014(tmp_path: Path) -> None:
    dag = {
        "extract": _node("dup", "dup", "dup"),
        "notify": _node("dup", "dup", "dup"),
    }
    findings = _run(tmp_path, {"miner/manifest.json": dag}, base="contract/generated")
    k014 = _ids(findings, "K014")
    assert len(k014) == 1
    assert k014[0].file == "contract/generated/miner/manifest.json"


def test_contract_generated_consistent_is_clean(tmp_path: Path) -> None:
    crawler = {
        "extract": _node("powerbi-crawler", "powerbi-crawler", "powerbi-crawler"),
        "publish": _node("publish", "publish", "publish"),
    }
    assert (
        _run(tmp_path, {"crawler/manifest.json": crawler}, base="contract/generated")
        == []
    )


def test_malformed_manifest_is_ignored(tmp_path: Path) -> None:
    generated = tmp_path / "app" / "generated"
    generated.mkdir(parents=True)
    (generated / "manifest.json").write_text("{ not json", encoding="utf-8")
    assert scan_all([], tmp_path) == []


def test_manifest_without_dag_is_ignored(tmp_path: Path) -> None:
    generated = tmp_path / "app" / "generated"
    generated.mkdir(parents=True)
    (generated / "manifest.json").write_text(json.dumps({"execution_mode": "x"}))
    assert scan_all([], tmp_path) == []
