"""Tests for .github/scripts/validate_atlan_yaml.py CI driver.

Covers the driver's branching logic: empty config, missing SDK version,
invalid SDK version, valid pass, valid fail. Asserts exit code + stdout
shape (GitHub Actions ::error annotations and human-readable log lines).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from validate_atlan_yaml import main


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Driver reads DEPLOY_CONFIG / SDK_VERSION from env. Clear before each
    test so leaked state from a prior run can't make a test pass for the
    wrong reason."""
    monkeypatch.delenv("DEPLOY_CONFIG", raising=False)
    monkeypatch.delenv("SDK_VERSION", raising=False)


def test_empty_config_returns_zero_with_skip_message(monkeypatch, capsys):
    monkeypatch.setenv("DEPLOY_CONFIG", "")
    monkeypatch.setenv("SDK_VERSION", "2.8.2")
    rc = main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipping validation" in out.lower()


def test_whitespace_only_config_returns_zero(monkeypatch, capsys):
    monkeypatch.setenv("DEPLOY_CONFIG", "   \n  \n")
    monkeypatch.setenv("SDK_VERSION", "2.8.2")
    rc = main()
    assert rc == 0
    assert "skipping" in capsys.readouterr().out.lower()


def test_missing_sdk_version_returns_zero_skip(monkeypatch, capsys):
    # Without SDK version, version-gated injection cannot run → validator's
    # view diverges from chart's view → skip rather than emit false positives.
    monkeypatch.setenv("DEPLOY_CONFIG", "replicaCount: 1\n")
    monkeypatch.delenv("SDK_VERSION", raising=False)
    rc = main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "no application-sdk version" in out.lower()


def test_empty_sdk_version_returns_zero_skip(monkeypatch, capsys):
    # `(env or '').strip() or None` path — empty/whitespace SDK_VERSION
    # collapses to None, same skip semantics as missing.
    monkeypatch.setenv("DEPLOY_CONFIG", "replicaCount: 1\n")
    monkeypatch.setenv("SDK_VERSION", "   ")
    rc = main()
    assert rc == 0
    assert "no application-sdk version" in capsys.readouterr().out.lower()


def test_invalid_sdk_version_returns_one_with_annotation(monkeypatch, capsys):
    # Unparseable PEP 440 → hard fail. Silent skip would let validation run
    # against un-enriched config, which diverges from chart behaviour.
    monkeypatch.setenv("DEPLOY_CONFIG", "replicaCount: 1\n")
    monkeypatch.setenv("SDK_VERSION", "not-a-version")
    rc = main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "::error file=atlan.yaml::" in out
    assert "not-a-version" in out
    assert "PEP 440" in out


def test_valid_config_returns_zero_with_pass_message(monkeypatch, capsys):
    monkeypatch.setenv(
        "DEPLOY_CONFIG",
        "replicaCount: 1\n"
        "resources:\n"
        "  requests: {cpu: 100m, memory: 500Mi}\n"
        "  limits:   {cpu: 1,    memory: 1Gi}\n",
    )
    monkeypatch.setenv("SDK_VERSION", "2.8.2")
    rc = main()
    assert rc == 0
    assert "validation passed" in capsys.readouterr().out


def test_invalid_config_returns_one_with_per_violation_annotations(monkeypatch, capsys):
    # Multiple violations should each get one ::error annotation + one
    # human-readable log line.
    monkeypatch.setenv(
        "DEPLOY_CONFIG",
        "resources:\n"
        "  requests: {cpu: 8, memory: 30Gi}\n"
        "  limits:   {cpu: 1, memory: 2Gi}\n"
        "keda:\n  minReplicaCount: 5\n  maxReplicaCount: 2\n",
    )
    monkeypatch.setenv("SDK_VERSION", "2.8.2")
    rc = main()
    assert rc == 1
    out = capsys.readouterr().out
    # Annotation per violation
    annotations = [
        line
        for line in out.splitlines()
        if line.startswith("::error file=atlan.yaml::")
    ]
    assert (
        len(annotations) >= 3
    )  # requests_cpu_ceiling + requests_memory_ceiling + keda_min_le_max
    # Specific rules surfaced
    joined = "\n".join(annotations)
    assert "requests_cpu_ceiling" in joined
    assert "requests_memory_ceiling" in joined
    assert "keda_min_le_max" in joined
    # Per-violation human-readable log line
    assert "violation(s)" in out


def test_quoted_bool_caught_by_invalid_type(monkeypatch, capsys):
    # Regression: quoted "true" must NOT silently bypass validation.
    monkeypatch.setenv(
        "DEPLOY_CONFIG",
        'splitDeploymentEnabled: "true"\ntemporalWorkerDeployment:\n  enabled: true\n',
    )
    monkeypatch.setenv("SDK_VERSION", "2.8.2")
    rc = main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "invalid_type" in out
    assert "splitDeploymentEnabled" in out


def test_twc_floor_violation_surfaces_via_driver(monkeypatch, capsys):
    # SDK < 2.7.4 with TWC enabled → twc_requires_sdk_2_7_4.
    monkeypatch.setenv(
        "DEPLOY_CONFIG",
        "temporalWorkerDeployment:\n  enabled: true\n",
    )
    monkeypatch.setenv("SDK_VERSION", "2.6.0")
    rc = main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "twc_requires_sdk_2_7_4" in out
    assert "2.7.4" in out


def test_sdk_injection_enriches_before_validation(monkeypatch, capsys):
    # SDK 2.8.2 injects splitDeploymentEnabled=true. With the chart's default
    # vpa.maxAllowed (cpu=2, mem=18Gi) and user's request of cpu=3, validator
    # must fire requests_exceeds_vpa_max_cpu — proving injection ran.
    monkeypatch.setenv(
        "DEPLOY_CONFIG",
        "resources:\n"
        "  requests: {cpu: 3, memory: 500Mi}\n"
        "  limits:   {cpu: 3, memory: 1Gi}\n",
    )
    monkeypatch.setenv("SDK_VERSION", "2.8.2")
    rc = main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "requests_exceeds_vpa_max_cpu" in out
