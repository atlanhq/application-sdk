"""Tests for .github/scripts/config_validator.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from config_validator import (
    ConfigValidationError,
    parse_cpu,
    parse_memory,
    validate_config,
)


# ---------------------------------------------------------------------------
# Quantity parsers
# ---------------------------------------------------------------------------

class TestParseCpu:
    @pytest.mark.parametrize("value,expected", [
        ("100m", 100),
        ("1", 1000),
        ("1.5", 1500),
        ("500m", 500),
        ("7", 7000),
        (1, 1000),
        (2, 2000),
        (0.5, 500),
    ])
    def test_valid(self, value, expected):
        assert parse_cpu(value) == expected

    @pytest.mark.parametrize("value", ["abc", "1x", "", True, False])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            parse_cpu(value)


class TestParseMemory:
    @pytest.mark.parametrize("value,expected", [
        ("500Mi", 500 * 1024 ** 2),
        ("1Gi", 1024 ** 3),
        ("27Gi", 27 * 1024 ** 3),
        ("1024", 1024),
        ("1Ki", 1024),
        ("1M", 1000 ** 2),
        ("1G", 1000 ** 3),
        ("1k", 1000),
        (1024, 1024),
    ])
    def test_valid(self, value, expected):
        assert parse_memory(value) == expected

    def test_27gi_equals_27648_mi(self):
        # User-stated invariant: 27Gi binary == 27 * 1024 Mi (NOT 27000 Mi).
        assert parse_memory("27Gi") == parse_memory("27648Mi")

    @pytest.mark.parametrize("value", ["abc", "1xi", "", True])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            parse_memory(value)


# ---------------------------------------------------------------------------
# validate_config — happy paths
# ---------------------------------------------------------------------------

class TestValidateHappyPaths:
    def test_empty_config_is_noop(self):
        validate_config("")
        validate_config(None)

    def test_dict_input_skips_yaml_parse(self):
        # validate_config accepts an already-parsed dict.
        with pytest.raises(ConfigValidationError) as exc:
            validate_config({
                "splitDeploymentEnabled": True,
                "temporalWorkerDeployment": {"enabled": False},
            })
        assert any(v.rule == "twc_required_for_split" for v in exc.value.violations)

    def test_non_mapping_input_is_noop(self):
        validate_config([])
        validate_config("- a\n- b\n")

    def test_minimal_valid_config(self):
        validate_config("""
replicaCount: 1
resources:
  requests:
    cpu: 100m
    memory: 500Mi
  limits:
    cpu: 1
    memory: 1Gi
""")

    def test_split_with_twc_enabled(self):
        validate_config("""
splitDeploymentEnabled: true
temporalWorkerDeployment:
  enabled: true
resources:
  requests: {cpu: 100m, memory: 500Mi}
  limits:   {cpu: 1,    memory: 1Gi}
serverResources:
  requests: {cpu: 100m, memory: 512Mi}
  limits:   {cpu: 300m, memory: 1Gi}
workerResources:
  requests: {cpu: 500m, memory: 1Gi}
  limits:   {cpu: 2,    memory: 4Gi}
""")

    def test_vpa_at_ceiling_passes(self):
        validate_config("""
vpa:
  enabled: true
  maxAllowed:
    cpu: 7
    memory: 27Gi
""")


# ---------------------------------------------------------------------------
# Rule: split requires TWC
# ---------------------------------------------------------------------------

class TestSplitRequiresTwc:
    def test_split_without_twd_passes(self):
        # SDK < 2.7.4: split injected, TWC block absent. Must NOT block —
        # TWC is only available on SDK >= 2.7.4. Rule fires only on the
        # explicit opt-out (enabled: false), not on missing fields.
        validate_config("splitDeploymentEnabled: true\n")

    def test_split_with_empty_twd_block_passes(self):
        # `temporalWorkerDeployment: {}` (no `enabled` key) treated same as
        # missing field — only explicit `enabled: false` fails.
        validate_config("""
splitDeploymentEnabled: true
temporalWorkerDeployment: {}
""")

    def test_split_with_twd_enabled_true_passes(self):
        validate_config("""
splitDeploymentEnabled: true
temporalWorkerDeployment:
  enabled: true
""")

    def test_split_with_twd_disabled_fails(self):
        # Explicit opt-out is the only case that blocks.
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
splitDeploymentEnabled: true
temporalWorkerDeployment:
  enabled: false
""")
        rules = {v.rule for v in exc.value.violations}
        assert "twc_required_for_split" in rules

    def test_no_split_no_twd_passes(self):
        validate_config("splitDeploymentEnabled: false\n")


# ---------------------------------------------------------------------------
# Rule: VPA maxAllowed ceilings
# ---------------------------------------------------------------------------

class TestVpaCaps:
    def test_cpu_above_ceiling_fails(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("vpa:\n  maxAllowed:\n    cpu: 8\n")
        assert any(v.rule == "vpa_max_cpu_ceiling" for v in exc.value.violations)

    def test_cpu_in_millicores_above_ceiling_fails(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("vpa:\n  maxAllowed:\n    cpu: 7001m\n")
        assert any(v.rule == "vpa_max_cpu_ceiling" for v in exc.value.violations)

    def test_memory_above_ceiling_fails(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("vpa:\n  maxAllowed:\n    memory: 28Gi\n")
        assert any(v.rule == "vpa_max_memory_ceiling" for v in exc.value.violations)

    def test_memory_27gi_passes(self):
        validate_config("vpa:\n  maxAllowed:\n    memory: 27Gi\n")

    def test_memory_27000mi_passes_but_27649mi_fails(self):
        # 27Gi binary == 27648 Mi. 27000 Mi < 27Gi → pass. 27649 Mi > 27Gi → fail.
        validate_config("vpa:\n  maxAllowed:\n    memory: 27000Mi\n")
        with pytest.raises(ConfigValidationError):
            validate_config("vpa:\n  maxAllowed:\n    memory: 27649Mi\n")


# ---------------------------------------------------------------------------
# Rule: requests <= ceiling
# ---------------------------------------------------------------------------

class TestRequestsCeiling:
    def test_cpu_request_above_ceiling_fails(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
resources:
  requests: {cpu: 8,    memory: 500Mi}
  limits:   {cpu: 8,    memory: 1Gi}
""")
        rules = {v.rule for v in exc.value.violations}
        assert "requests_cpu_ceiling" in rules

    def test_memory_request_above_ceiling_fails(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
resources:
  requests: {cpu: 100m, memory: 28Gi}
  limits:   {cpu: 1,    memory: 30Gi}
""")
        rules = {v.rule for v in exc.value.violations}
        assert "requests_memory_ceiling" in rules

    def test_request_at_ceiling_passes(self):
        validate_config("""
resources:
  requests: {cpu: 7,    memory: 27Gi}
  limits:   {cpu: 7,    memory: 27Gi}
""")

    def test_split_ceiling_applies_to_server_and_worker(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
splitDeploymentEnabled: true
temporalWorkerDeployment: {enabled: true}
resources:
  requests: {cpu: 100m, memory: 500Mi}
  limits:   {cpu: 1,    memory: 1Gi}
workerResources:
  requests: {cpu: 8,    memory: 28Gi}
  limits:   {cpu: 8,    memory: 30Gi}
""")
        fields = {v.field for v in exc.value.violations}
        assert "workerResources.requests.cpu" in fields
        assert "workerResources.requests.memory" in fields


# ---------------------------------------------------------------------------
# Rule: when vpa.enabled, requests <= vpa.maxAllowed
# ---------------------------------------------------------------------------

class TestRequestsLeVpaMax:
    def test_cpu_request_above_default_max_when_vpa_enabled_no_max_declared(self):
        # vpa.enabled=true and no maxAllowed → defaults apply (cpu 2, mem 18Gi).
        # Request of 3 cores exceeds the 2-core default.
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
vpa:
  enabled: true
resources:
  requests: {cpu: 3,    memory: 500Mi}
  limits:   {cpu: 3,    memory: 1Gi}
""")
        rules = {v.rule for v in exc.value.violations}
        assert "requests_exceeds_vpa_max_cpu" in rules

    def test_memory_request_above_default_max(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
vpa:
  enabled: true
resources:
  requests: {cpu: 100m, memory: 19Gi}
  limits:   {cpu: 1,    memory: 19Gi}
""")
        rules = {v.rule for v in exc.value.violations}
        assert "requests_exceeds_vpa_max_memory" in rules

    def test_request_within_declared_max_passes(self):
        validate_config("""
vpa:
  enabled: true
  maxAllowed: {cpu: 5, memory: 20Gi}
resources:
  requests: {cpu: 4,    memory: 16Gi}
  limits:   {cpu: 4,    memory: 20Gi}
""")

    def test_request_above_declared_max_fails(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
vpa:
  enabled: true
  maxAllowed: {cpu: 1, memory: 4Gi}
resources:
  requests: {cpu: 2,    memory: 5Gi}
  limits:   {cpu: 2,    memory: 6Gi}
""")
        rules = {v.rule for v in exc.value.violations}
        assert "requests_exceeds_vpa_max_cpu" in rules
        assert "requests_exceeds_vpa_max_memory" in rules

    def test_vpa_disabled_skips_rule(self):
        # Request of 3 cores would violate default 2-core max — but vpa.enabled
        # is false, so the requests<=vpa.maxAllowed rule does not apply.
        validate_config("""
vpa:
  enabled: false
resources:
  requests: {cpu: 3,    memory: 500Mi}
  limits:   {cpu: 3,    memory: 1Gi}
""")

    def test_split_rule_applies_to_server_and_worker_when_vpa_enabled(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
splitDeploymentEnabled: true
temporalWorkerDeployment: {enabled: true}
vpa:
  enabled: true
resources:
  requests: {cpu: 100m, memory: 500Mi}
  limits:   {cpu: 1,    memory: 1Gi}
workerResources:
  requests: {cpu: 3,    memory: 500Mi}
  limits:   {cpu: 3,    memory: 1Gi}
""")
        fields = {v.field for v in exc.value.violations
                  if v.rule == "requests_exceeds_vpa_max_cpu"}
        assert "workerResources.requests.cpu" in fields


# ---------------------------------------------------------------------------
# Rule: requests <= limits
# ---------------------------------------------------------------------------

class TestRequestsLeLimits:
    def test_cpu_request_above_limit_fails(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
resources:
  requests: {cpu: 2,   memory: 500Mi}
  limits:   {cpu: 1,   memory: 1Gi}
""")
        assert any(v.rule == "requests_le_limits" for v in exc.value.violations)

    def test_memory_request_above_limit_fails(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
resources:
  requests: {cpu: 100m, memory: 1Gi}
  limits:   {cpu: 1,    memory: 500Mi}
""")
        assert any(v.rule == "requests_le_limits" for v in exc.value.violations)


# ---------------------------------------------------------------------------
# Rule: vpa.minAllowed <= maxAllowed
# ---------------------------------------------------------------------------

class TestVpaMinLeMax:
    def test_min_above_max_fails(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
vpa:
  minAllowed: {cpu: 2,   memory: 1Gi}
  maxAllowed: {cpu: 1,   memory: 2Gi}
""")
        assert any(v.rule == "vpa_min_le_max" for v in exc.value.violations)


# ---------------------------------------------------------------------------
# Rule: keda min <= max
# ---------------------------------------------------------------------------

class TestKedaMinLeMax:
    def test_min_above_max_fails(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("keda:\n  minReplicaCount: 5\n  maxReplicaCount: 2\n")
        assert any(v.rule == "keda_min_le_max" for v in exc.value.violations)

    def test_only_min_set_passes(self):
        validate_config("keda:\n  minReplicaCount: 0\n")

    def test_bool_replicas_skipped(self):
        # bool subclass of int — must not produce a Violation.
        validate_config("keda:\n  minReplicaCount: true\n  maxReplicaCount: false\n")


# ---------------------------------------------------------------------------
# Aggregation, dedup, error shape
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_multiple_violations_reported_in_one_pass(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
splitDeploymentEnabled: true
temporalWorkerDeployment:
  enabled: false
vpa:
  maxAllowed: {cpu: 8, memory: 28Gi}
keda:
  minReplicaCount: 5
  maxReplicaCount: 1
""")
        rules = {v.rule for v in exc.value.violations}
        assert {"twc_required_for_split", "vpa_max_cpu_ceiling",
                "vpa_max_memory_ceiling", "keda_min_le_max"} <= rules

    def test_violation_is_serialisable(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("vpa:\n  maxAllowed:\n    cpu: 8\n")
        for v in exc.value.violations:
            d = v.to_dict()
            assert set(d.keys()) == {"field", "actual", "expected", "rule", "fix"}

    def test_inherits_value_error(self):
        with pytest.raises(ValueError):
            validate_config("vpa:\n  maxAllowed:\n    cpu: 8\n")


class TestParseOnceDedup:
    def test_invalid_memory_emits_single_violation_per_field(self):
        # Without parse-once cache, invalid memory string would emit two
        # `invalid_quantity` violations on the same field.
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
resources:
  requests: {memory: "abc"}
  limits:   {memory: "xyz"}
""")
        invalid = [v for v in exc.value.violations if v.rule == "invalid_quantity"]
        fields = [v.field for v in invalid]
        assert fields.count("resources.requests.memory") == 1
        assert fields.count("resources.limits.memory") == 1

    def test_invalid_vpa_max_emits_single_violation_per_field(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
vpa:
  minAllowed: {memory: 1Gi}
  maxAllowed: {memory: "abc"}
""")
        invalid = [v for v in exc.value.violations if v.rule == "invalid_quantity"]
        assert [v.field for v in invalid].count("vpa.maxAllowed.memory") == 1


class TestYamlParseFailure:
    def test_invalid_yaml_raises_validation_error(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("foo: [unterminated")
        assert any(v.rule == "yaml_parse" for v in exc.value.violations)
