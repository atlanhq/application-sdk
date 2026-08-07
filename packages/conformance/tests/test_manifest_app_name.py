"""Tests for K013 ManifestNodeAppNameMisattributed.

Covers the "toolkit-owned workflow filed under Automation Engine" check: a DAG
node in a committed generated ``manifest.json`` whose ``inputs.workflow_type``
is a workflow the toolkit owns (QI / publish / lineage / popularity /
notification) must not carry the raw ``DAGNode`` default ``app_name`` of
``automation-engine`` — AE does not run those workflows, so the pairing always
misattributes the node's telemetry (CNCT-24).

Test helpers
------------
``_write_manifest``: writes a ``manifest.json`` with a given ``dag`` dict at a
repo-relative path under ``tmp_path``.
``_node``: builds one DAG node with a chosen ``workflow_type`` / ``app_name``,
stamping the value at all three positions the toolkit renders it at.
``_run``: calls :func:`scan_all` against ``tmp_path`` and returns its findings.
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
    workflow_type: str,
    app_name: str,
    *,
    task_queue: str = "some-queue",
) -> dict:
    """One DAG node, with ``app_name`` at each position the toolkit renders."""
    return {
        "activity_name": "execute_workflow",
        "activity_display_name": "A Step",
        "app_name": app_name,
        "inputs": {
            "workflow_type": workflow_type,
            "app_name": app_name,
            "task_queue": task_queue,
            "args": {"app_name": app_name},
        },
    }


def _write_manifest(tmp_path: Path, rel: str, dag: dict) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dag": dag}, indent=2), encoding="utf-8")


def _run(tmp_path: Path) -> list:
    return scan_all([], tmp_path)


# ---------------------------------------------------------------------------
# Rule registration
# ---------------------------------------------------------------------------


def test_k013_registered_as_app_scoped_warn() -> None:
    rule = get_rule("K013")
    assert rule.name == "ManifestNodeAppNameMisattributed"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"
    assert rule.rationale


# ---------------------------------------------------------------------------
# Positive cases — the misattribution this rule exists to catch
# ---------------------------------------------------------------------------


def test_qi_node_defaulted_to_automation_engine_is_flagged(tmp_path: Path) -> None:
    """The motivating shape: a hand-written QI node inheriting the AE default."""
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "extract": _node("myapp:extract", "myapp"),
            "qi": _node("QueryIntelligenceWorkflow", "automation-engine"),
        },
    )

    findings = _run(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "K013"
    assert finding.file == "app/generated/manifest.json"
    assert "'qi'" in finding.message
    assert "query-intelligence" in finding.message
    assert "QueryIntelligenceNode" in finding.message
    assert not finding.suppressed


def test_every_toolkit_owned_workflow_type_is_covered(tmp_path: Path) -> None:
    """Each built-in workflow names its own app, not AE."""
    expected = {
        "QueryIntelligenceWorkflow": "query-intelligence",
        "PublishWorkflow": "publish",
        "LineageWorkflow": "lineage",
        "PopularityWorkflow": "popularity",
        "NotificationWorkflow": "notification-app",
    }
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            f"node{i}": _node(workflow_type, "automation-engine")
            for i, workflow_type in enumerate(expected)
        },
    )

    findings = _run(tmp_path)

    assert len(findings) == len(expected)
    for app_name in expected.values():
        assert any(app_name in f.message for f in findings), app_name


def test_contract_generated_tree_is_scanned_too(tmp_path: Path) -> None:
    """Apps that commit ``contract/generated/`` are covered at both roots."""
    dag = {"qi": _node("QueryIntelligenceWorkflow", "automation-engine")}
    _write_manifest(tmp_path, "app/generated/manifest.json", dag)
    _write_manifest(tmp_path, "contract/generated/manifest.json", dag)

    findings = _run(tmp_path)

    assert {f.file for f in findings} == {
        "app/generated/manifest.json",
        "contract/generated/manifest.json",
    }


def test_multi_entrypoint_bundle_manifests_are_scanned(tmp_path: Path) -> None:
    """Per-entrypoint manifests live in subfolders; rglob must reach them."""
    _write_manifest(
        tmp_path,
        "app/generated/crawler/manifest.json",
        {"qi": _node("QueryIntelligenceWorkflow", "automation-engine")},
    )

    findings = _run(tmp_path)

    assert [f.file for f in findings] == ["app/generated/crawler/manifest.json"]


def test_finding_anchors_on_the_offending_nodes_app_name_line(
    tmp_path: Path,
) -> None:
    """The reported line is inside the flagged node, not the first node."""
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "extract": _node("myapp:extract", "myapp"),
            "qi": _node("QueryIntelligenceWorkflow", "automation-engine"),
        },
    )
    text = (tmp_path / "app/generated/manifest.json").read_text(encoding="utf-8")
    lines = text.splitlines()

    finding = _run(tmp_path)[0]

    assert '"app_name"' in lines[finding.line - 1]
    qi_line = next(i for i, ln in enumerate(lines, start=1) if '"qi"' in ln)
    assert finding.line > qi_line


def test_app_name_only_at_the_inputs_position_is_still_flagged(
    tmp_path: Path,
) -> None:
    """An older manifest shape lacking the top-level key is not a blind spot."""
    node = _node("PublishWorkflow", "automation-engine")
    del node["app_name"]
    _write_manifest(tmp_path, "app/generated/manifest.json", {"publish": node})

    findings = _run(tmp_path)

    assert len(findings) == 1
    assert "publish" in findings[0].message


def test_app_name_only_inside_inputs_args_is_still_flagged(tmp_path: Path) -> None:
    """A pre-CNCT-93 manifest may carry the value only in ``inputs.args``."""
    node = _node("LineageWorkflow", "automation-engine")
    del node["app_name"]
    del node["inputs"]["app_name"]
    _write_manifest(tmp_path, "app/generated/manifest.json", {"lineage": node})

    findings = _run(tmp_path)

    assert len(findings) == 1
    assert "lineage" in findings[0].message


# ---------------------------------------------------------------------------
# Signal 2 — the task queue names the owning system app
# ---------------------------------------------------------------------------


def test_system_app_queue_disagreeing_with_app_name_is_flagged(
    tmp_path: Path,
) -> None:
    """A queue naming a system app contradicts a different ``app_name``.

    Independent of ``workflow_type`` — this node's type is the connector's own,
    so signal 1 says nothing about it.
    """
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "qi": _node(
                "someconnector:parse",
                "someconnector",
                task_queue="atlan-query-intelligence-production",
            )
        },
    )

    findings = _run(tmp_path)

    assert len(findings) == 1
    message = findings[0].message
    assert "query-intelligence" in message
    assert "atlan-query-intelligence-production" in message
    # Log identity only — the rule must never ask for a routing change.
    assert "task_queue is the routing decision" in message


def test_deployment_name_placeholder_suffix_is_not_interpreted(
    tmp_path: Path,
) -> None:
    """The app segment is matched; the suffix may be a template or an env word."""
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "publish": _node(
                "someconnector:publish",
                "someconnector",
                task_queue="atlan-publish-{deployment_name}",
            )
        },
    )

    findings = _run(tmp_path)

    assert len(findings) == 1
    assert "publish" in findings[0].message


def test_hyphenated_deployment_suffix_still_identifies_the_owner(
    tmp_path: Path,
) -> None:
    """A multi-segment suffix (``-production-us-east-1``) must not no-op signal 2.

    Regression: a lazy ``.+?`` app group with a hyphenless-suffix anchor
    mis-captured ``query-intelligence-production-us-east`` here — not a known
    app, so the queue signal silently concluded nothing on exactly the
    deployment-suffixed queues real system apps carry.
    """
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "qi": _node(
                "someconnector:parse",
                "someconnector",
                task_queue="atlan-query-intelligence-production-us-east-1",
            )
        },
    )

    findings = _run(tmp_path)

    assert len(findings) == 1
    assert "query-intelligence" in findings[0].message
    assert "atlan-query-intelligence-production-us-east-1" in findings[0].message


def test_hyphenated_app_name_with_hyphenated_suffix_is_flagged(
    tmp_path: Path,
) -> None:
    """``notification-app`` is itself hyphenated and must not be shadowed."""
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "notify": _node(
                "someconnector:notify",
                "someconnector",
                task_queue="atlan-notification-app-production-us-east-1",
            )
        },
    )

    findings = _run(tmp_path)

    assert len(findings) == 1
    assert "notification-app" in findings[0].message


def test_connector_own_queue_is_never_flagged(tmp_path: Path) -> None:
    """A connector's own queue says nothing about system-app ownership."""
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "process": _node(
                "domo-app:process-metadata",
                "domo",
                task_queue="atlan-domo-{deployment_name}",
            )
        },
    )

    assert _run(tmp_path) == []


def test_queue_agreeing_with_app_name_is_clean(tmp_path: Path) -> None:
    """The correct shape a built-in node class renders."""
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "qi": _node(
                "QueryIntelligenceWorkflow",
                "query-intelligence",
                task_queue="atlan-query-intelligence-{deployment_name}",
            )
        },
    )

    assert _run(tmp_path) == []


def test_unparseable_queue_is_skipped(tmp_path: Path) -> None:
    """A queue that does not match the atlan-<app>-<suffix> shape concludes nothing."""
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {"custom": _node("some:workflow", "someconnector", task_queue="my-own-queue")},
    )

    assert _run(tmp_path) == []


def test_explicit_non_default_app_name_on_builtin_workflow_is_left_alone(
    tmp_path: Path,
) -> None:
    """Signal 1 fires only on the AE default; a bespoke worker is the author's call.

    Guards against the rule creeping into overriding deliberate author intent —
    the reason this is a detection rule and not a generation-time rewrite.
    """
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "qi": _node(
                "QueryIntelligenceWorkflow",
                "custom-qi-worker",
                task_queue="atlan-custom-qi-worker-{deployment_name}",
            )
        },
    )

    assert _run(tmp_path) == []


def test_one_finding_per_node_when_both_signals_agree(tmp_path: Path) -> None:
    """The real-world shape: both signals point at the same app. Report once."""
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "qi": _node(
                "QueryIntelligenceWorkflow",
                "automation-engine",
                task_queue="atlan-query-intelligence-production",
            )
        },
    )

    findings = _run(tmp_path)

    assert len(findings) == 1
    assert "query-intelligence" in findings[0].message


# ---------------------------------------------------------------------------
# Negative cases — no finding
# ---------------------------------------------------------------------------


def test_correctly_attributed_toolkit_node_is_clean(tmp_path: Path) -> None:
    """What the built-in node classes (and the fixed toolkit) render."""
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {
            "qi": _node("QueryIntelligenceWorkflow", "query-intelligence"),
            "publish": _node("PublishWorkflow", "publish"),
        },
    )

    assert _run(tmp_path) == []


def test_app_owned_node_on_automation_engine_is_not_flagged(tmp_path: Path) -> None:
    """A node genuinely hosted by AE keeps the default legitimately.

    The rule keys on the toolkit-owned workflow types only — it must never
    treat ``app_name == "automation-engine"`` as suspicious on its own.
    """
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {"custom": _node("SomeAeHostedWorkflow", "automation-engine")},
    )

    assert _run(tmp_path) == []


def test_no_generated_tree_is_a_noop(tmp_path: Path) -> None:
    assert _run(tmp_path) == []


def test_malformed_manifest_is_skipped(tmp_path: Path) -> None:
    """Unparseable JSON yields a false negative, never a crash."""
    path = tmp_path / "app/generated/manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert _run(tmp_path) == []


def test_manifest_without_dag_object_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "app/generated/manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"execution_mode": "automation-engine"}), "utf-8")

    assert _run(tmp_path) == []


def test_non_dict_node_entry_is_skipped(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "app/generated/manifest.json", {"weird": "a string"})

    assert _run(tmp_path) == []


def test_node_without_inputs_is_skipped(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        "app/generated/manifest.json",
        {"qi": {"activity_name": "execute_workflow", "app_name": "automation-engine"}},
    )

    assert _run(tmp_path) == []
