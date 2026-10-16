"""Tests for O009 WorkerVolumeWithoutAutoresize.

The check reads ``atlan.yaml`` line by line, so the layouts the fleet actually
writes are pinned: items indented under their key (hand-written files) and items
flush with it (the contract toolkit's YAML renderer).  Fixtures follow those two
real shapes — the indented one as hand-written in atlan-databricks-app's
``atlan.yaml``, the flush one as rendered into atlan-snowflake-app's generated
``atlan.yaml`` — with synthetic names and sizes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite import runner
from conformance.suite.checks import worker_volume
from conformance.suite.checks.worker_volume import (
    discover,
    main,
    scan_path,
    scan_text,
    storage_entries,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.catalog import RemediationKind
from conformance.suite.schema.disposition import EnforcementTier, FixLocus, RuleScope

# Hand-written layout: sequence items indented under the key.
_INDENTED_FIXED = """\
name: demo
deploy:
  dapr:
    objectstore: true
  storage:
    - name: local-data
      mountPath: /app/local
      size: 300Gi
  securityContext:
    fsGroup: 1000
"""

# Toolkit-rendered layout: sequence items flush with the key.
_FLUSH_AUTORESIZE = """\
# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.
name: demo
deploy:
  execution_mode: native
  storage:
  - name: local-data
    mountPath: /app/local
    size: 8Gi
    annotations:
      resize.topolvm.io/storage_limit: 192Gi
      resize.topolvm.io/threshold: 50%
      resize.topolvm.io/increase: 8Gi
  temporalWorkerDeployment:
    enabled: true
pools:
  default:
    keda:
      enabled: true
"""


def _lines(text: str) -> list[int]:
    return [f.line for f in scan_text(text, "atlan.yaml")]


# ── fires ──────────────────────────────────────────────────────────────────────


def test_fires_on_a_fixed_size_storage_entry() -> None:
    (finding,) = scan_text(_INDENTED_FIXED, "atlan.yaml")
    assert finding.rule_id == "O009"
    assert (finding.file, finding.line, finding.column) == ("atlan.yaml", 6, 5)
    for needle in (
        "deploy.storage entry 'local-data'",
        "mountPath /app/local",
        "fixed 300Gi volume",
        "resize.topolvm.io/storage_limit",
        "about 1% of pods",
        "contract/app.pkl",
    ):
        assert needle in finding.message


def test_fires_on_a_flush_layout_entry_without_the_limit() -> None:
    text = _FLUSH_AUTORESIZE.replace(
        "      resize.topolvm.io/storage_limit: 192Gi\n", ""
    )
    assert _lines(text) == [6]


def test_fires_on_storage_worker_and_names_worker_pods() -> None:
    text = (
        "deploy:\n"
        "  storageWorker:\n"
        "    - name: scratch\n"
        "      mountPath: /tmp\n"
        "      size: 64Gi\n"
    )
    (finding,) = scan_text(text, "atlan.yaml")
    assert finding.line == 3
    assert "deploy.storageWorker entry 'scratch'" in finding.message
    assert "every worker pod" in finding.message


def test_fires_when_annotations_lack_the_storage_limit() -> None:
    text = (
        "deploy:\n"
        "  storage:\n"
        "    - name: local-data\n"
        "      size: 64Gi\n"
        "      annotations:\n"
        "        resize.topolvm.io/threshold: '25%'\n"
        "        resize.topolvm.io/increase: 16Gi\n"
    )
    assert _lines(text) == [3]


@pytest.mark.parametrize("value", ['""', "''", "null", "~", ""])
def test_fires_on_an_empty_storage_limit(value: str) -> None:
    text = (
        "deploy:\n"
        "  storage:\n"
        "    - name: local-data\n"
        "      size: 64Gi\n"
        "      annotations:\n"
        f"        resize.topolvm.io/storage_limit: {value}\n"
    )
    assert _lines(text) == [3]


def test_a_commented_out_limit_does_not_count() -> None:
    text = (
        "deploy:\n"
        "  storage:\n"
        "    - name: local-data\n"
        "      size: 64Gi\n"
        "      annotations:\n"
        "        # resize.topolvm.io/storage_limit: 192Gi\n"
        "        resize.topolvm.io/increase: 16Gi\n"
    )
    assert _lines(text) == [3]


def test_grades_each_entry_on_its_own_line() -> None:
    text = (
        "deploy:\n"
        "  storage:\n"
        "    - name: grows\n"
        "      size: 8Gi\n"
        "      annotations:\n"
        "        resize.topolvm.io/storage_limit: 192Gi\n"
        "    - name: fixed\n"
        "      size: 100Gi\n"
        "  storageWorker:\n"
        "  - name: worker-fixed\n"
        "    size: 50Gi\n"
    )
    findings = scan_text(text, "atlan.yaml")
    assert [(f.line, f.message.split(" declares")[0]) for f in findings] == [
        (7, "deploy.storage entry 'fixed'"),
        (10, "deploy.storageWorker entry 'worker-fixed'"),
    ]


def test_an_entry_with_no_size_still_fires() -> None:
    text = "deploy:\n  storage:\n    - name: local-data\n      mountPath: /data\n"
    (finding,) = scan_text(text, "atlan.yaml")
    assert "a fixed volume (no size)" in finding.message


def test_keys_may_start_on_the_line_after_the_dash() -> None:
    text = "deploy:\n  storage:\n    -\n      name: local-data\n      size: 10Gi\n"
    assert [(e.line, e.name, e.size) for e in storage_entries(text)] == [
        (3, "local-data", "10Gi")
    ]


# ── silent ─────────────────────────────────────────────────────────────────────


def test_silent_on_a_flush_layout_entry_with_autoresize() -> None:
    assert _lines(_FLUSH_AUTORESIZE) == []


def test_silent_on_an_indented_entry_with_autoresize() -> None:
    text = _INDENTED_FIXED.replace(
        "      size: 300Gi\n",
        "      size: 8Gi\n"
        "      annotations:\n"
        "        resize.topolvm.io/storage_limit: 192Gi\n",
    )
    assert _lines(text) == []


@pytest.mark.parametrize(
    "annotations",
    [
        '      annotations:\n        "resize.topolvm.io/storage_limit": "192Gi"\n',
        "      annotations: {resize.topolvm.io/storage_limit: 192Gi, x: y}\n",
        (
            "      annotations:  # pvc-autoresizer\n"
            "        resize.topolvm.io/storage_limit: 192Gi  # ceiling\n"
        ),
    ],
    ids=["quoted-key", "flow-mapping", "trailing-comments"],
)
def test_silent_on_other_spellings_of_the_limit(annotations: str) -> None:
    text = (
        "deploy:\n  storage:\n    - name: local-data\n      size: 8Gi\n" + annotations
    )
    assert _lines(text) == []


def test_silent_on_server_only_storage() -> None:
    text = "deploy:\n  storageServer:\n    - name: cache\n      size: 100Gi\n"
    assert _lines(text) == []


def test_silent_on_a_nested_storage_key() -> None:
    text = (
        "deploy:\n"
        "  extraVolumes:\n"
        "    - name: app-data\n"
        "      ephemeral:\n"
        "        volumeClaimTemplate:\n"
        "          spec:\n"
        "            resources:\n"
        "              requests:\n"
        "                storage: 50Gi\n"
        "  workerPools:\n"
        "    - name: heavy\n"
        "      storage:\n"
        "        - name: pool-data\n"
        "          size: 20Gi\n"
    )
    assert _lines(text) == []


def test_silent_on_storage_outside_the_deploy_block() -> None:
    text = (
        "storage:\n"
        "  - name: top-level\n"
        "    size: 10Gi\n"
        "deploy:\n"
        "  execution_mode: native\n"
        "pools:\n"
        "  storage:\n"
        "  - name: not-deploy\n"
        "    size: 10Gi\n"
    )
    assert _lines(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "name: demo\n",
        "deploy:\n  storage: []\n",
        "deploy:\n  storage:\n  splitDeploymentEnabled: true\n",
        "deploy:\n  storage:\n    name: not-a-list\n    size: 10Gi\n",
        "deploy:\n  storage:\n    - {name: flow, size: 10Gi}\n",
    ],
    ids=["no-deploy", "empty-flow", "empty-block", "mapping", "flow-entry"],
)
def test_silent_on_shapes_it_does_not_grade(text: str) -> None:
    assert _lines(text) == []


def test_a_mapping_value_does_not_hide_a_later_storage_key() -> None:
    text = (
        "deploy:\n"
        "  storage:\n"
        "    name: odd\n"
        "  storageWorker:\n"
        "    - name: scratch\n"
        "      size: 32Gi\n"
    )
    assert _lines(text) == [5]


# ── suppression ────────────────────────────────────────────────────────────────


def test_suppressed_by_a_directive_on_the_entry_line() -> None:
    text = _INDENTED_FIXED.replace(
        "    - name: local-data\n",
        "    - name: local-data  # conformance: ignore[O009] fixed size is contractual\n",
    )
    (finding,) = scan_text(text, "atlan.yaml")
    assert finding.suppressed is True


def test_suppressed_by_a_directive_on_the_line_above() -> None:
    text = _INDENTED_FIXED.replace(
        "    - name: local-data\n",
        "    # conformance: ignore[O009] fixed size is contractual\n"
        "    - name: local-data\n",
    )
    (finding,) = scan_text(text, "atlan.yaml")
    assert finding.suppressed is True


def test_a_directive_without_a_reason_does_not_suppress() -> None:
    text = _INDENTED_FIXED.replace(
        "    - name: local-data\n",
        "    - name: local-data  # conformance: ignore[O009]\n",
    )
    (finding,) = scan_text(text, "atlan.yaml")
    assert finding.suppressed is False


# ── discovery, entry points, metadata ──────────────────────────────────────────


def test_discover_returns_only_the_root_atlan_yaml(tmp_path: Path) -> None:
    assert discover(tmp_path) == []
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "atlan.yaml").write_text(_INDENTED_FIXED, encoding="utf-8")
    assert discover(tmp_path) == []
    (tmp_path / "atlan.yaml").write_text(_INDENTED_FIXED, encoding="utf-8")
    assert discover(tmp_path) == [tmp_path / "atlan.yaml"]


def test_scan_path_survives_undecodable_bytes(tmp_path: Path) -> None:
    path = tmp_path / "atlan.yaml"
    path.write_bytes(b"\xff\xfe deploy:\n")
    assert scan_path(path, tmp_path) == []


def test_cli_exits_zero_on_warn_findings(tmp_path: Path) -> None:
    (tmp_path / "atlan.yaml").write_text(_INDENTED_FIXED, encoding="utf-8")
    out = tmp_path / "out.sarif"
    code = main(["--root", str(tmp_path), str(tmp_path), "--sarif-output", str(out)])
    assert code == 0
    results = json.loads(out.read_text(encoding="utf-8"))["runs"][0]["results"]
    assert [r["ruleId"] for r in results] == ["O009"]


def _repo(tmp_path: Path, project: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "{project}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "atlan.yaml").write_text(_INDENTED_FIXED, encoding="utf-8")
    return tmp_path


def _results(repo: Path) -> list[tuple[str, str, int]]:
    out = repo / "out.sarif"
    runner.main(["--repo", str(repo), "--rule", "O009", "--output", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))
    return [
        (
            r["ruleId"],
            r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
            r["locations"][0]["physicalLocation"]["region"]["startLine"],
        )
        for r in doc["runs"][0]["results"]
    ]


def test_runner_reports_o009_on_an_app_repo(tmp_path: Path) -> None:
    assert _results(_repo(tmp_path, "atlan-demo-app")) == [("O009", "atlan.yaml", 6)]


def test_runner_drops_o009_on_the_sdk(tmp_path: Path) -> None:
    assert _results(_repo(tmp_path, "atlan-application-sdk")) == []


def test_runner_registers_the_check_under_the_o_series() -> None:
    registered = [c for c in runner._CHECKS if c.scan_path is worker_volume.scan_path]
    assert [c.series for c in registered] == ["O"]


def test_rule_metadata() -> None:
    rule = get_rule("O009")
    assert rule.name == "WorkerVolumeWithoutAutoresize"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.fix_locus is FixLocus.PACKAGING
    assert rule.autofixable is False
    assert rule.remediation_reference is not None
    assert rule.remediation_reference.kind is RemediationKind.GUIDE
    assert rule.remediation_reference.target == "programs/areas/optimizations.prose.md"
