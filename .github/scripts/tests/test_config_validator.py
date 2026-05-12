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
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("100m", 100),
            ("1", 1000),
            ("1.5", 1500),
            ("500m", 500),
            ("7", 7000),
            (1, 1000),
            (2, 2000),
            (0.5, 500),
        ],
    )
    def test_valid(self, value, expected):
        assert parse_cpu(value) == expected

    @pytest.mark.parametrize("value", ["abc", "1x", "", True, False])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            parse_cpu(value)


class TestParseMemory:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("500Mi", 500 * 1024**2),
            ("1Gi", 1024**3),
            ("27Gi", 27 * 1024**3),
            ("1024", 1024),
            ("1Ki", 1024),
            ("1M", 1000**2),
            ("1G", 1000**3),
            ("1k", 1000),
            (1024, 1024),
        ],
    )
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
            validate_config(
                {
                    "splitDeploymentEnabled": True,
                    "temporalWorkerDeployment": {"enabled": False},
                }
            )
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

    def test_update_mode_off_skips_clamp_rule(self):
        # VPA updateMode=Off → recommendations only, no admission clamp.
        # Same request that fires the rule with default mode must pass here.
        validate_config("""
vpa:
  enabled: true
  updateMode: "Off"
resources:
  requests: {cpu: 3,    memory: 500Mi}
  limits:   {cpu: 3,    memory: 1Gi}
""")

    def test_update_mode_off_case_insensitive(self):
        # Lowercase `off` also opts out — chart's schema rejects bad casing
        # downstream; here we mirror app-owner intent.
        validate_config("""
vpa:
  enabled: true
  updateMode: off
resources:
  requests: {cpu: 3,    memory: 500Mi}
  limits:   {cpu: 3,    memory: 1Gi}
""")

    def test_update_mode_auto_still_clamps(self):
        # Sanity: updateMode=Auto must still fire the rule.
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
vpa:
  enabled: true
  updateMode: "Auto"
resources:
  requests: {cpu: 3,    memory: 500Mi}
  limits:   {cpu: 3,    memory: 1Gi}
""")
        rules = {v.rule for v in exc.value.violations}
        assert "requests_exceeds_vpa_max_cpu" in rules

    def test_update_mode_initial_still_clamps(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config("""
vpa:
  enabled: true
  updateMode: "Initial"
resources:
  requests: {cpu: 3,    memory: 500Mi}
  limits:   {cpu: 3,    memory: 1Gi}
""")
        rules = {v.rule for v in exc.value.violations}
        assert "requests_exceeds_vpa_max_cpu" in rules

    def test_update_mode_off_split_resources_also_skipped(self):
        # workerResources / serverResources path under split must also honour
        # the Off opt-out — this is the exact scenario from prod.
        validate_config(
            """
splitDeploymentEnabled: true
temporalWorkerDeployment: {enabled: true}
vpa:
  enabled: true
  updateMode: "Off"
resources:
  requests: {cpu: 100m, memory: 500Mi}
  limits:   {cpu: 1,    memory: 1Gi}
workerResources:
  requests: {cpu: 3,    memory: 500Mi}
  limits:   {cpu: 3,    memory: 1Gi}
""",
            sdk_version="3.6.0",
        )

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
        fields = {
            v.field
            for v in exc.value.violations
            if v.rule == "requests_exceeds_vpa_max_cpu"
        }
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
        assert {
            "twc_required_for_split",
            "vpa_max_cpu_ceiling",
            "vpa_max_memory_ceiling",
            "keda_min_le_max",
        } <= rules

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


# ---------------------------------------------------------------------------
# Rule: invalid_type on non-bool flags
# ---------------------------------------------------------------------------


class TestInvalidBoolType:
    def test_quoted_split_flag_emits_invalid_type(self):
        # `splitDeploymentEnabled: "true"` is a string, not bool. Previously
        # `is True` silently bypassed every split-dependent rule. Must surface
        # invalid_type so the user sees the typo.
        with pytest.raises(ConfigValidationError) as exc:
            validate_config('splitDeploymentEnabled: "true"\n')
        fields = {(v.rule, v.field) for v in exc.value.violations}
        assert ("invalid_type", "splitDeploymentEnabled") in fields

    def test_quoted_vpa_enabled_emits_invalid_type(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config('vpa:\n  enabled: "true"\n')
        fields = {(v.rule, v.field) for v in exc.value.violations}
        assert ("invalid_type", "vpa.enabled") in fields

    def test_quoted_twd_enabled_emits_invalid_type(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config(
                "splitDeploymentEnabled: true\n"
                'temporalWorkerDeployment:\n  enabled: "false"\n'
            )
        rules = {v.rule for v in exc.value.violations}
        assert "invalid_type" in rules
        # twc_required_for_split must NOT fire on the string — only invalid_type.
        assert "twc_required_for_split" not in rules

    def test_quoted_vpa_enabled_skips_vpa_max_rule(self):
        # With `vpa.enabled: "true"` (string), the requests<=vpa.maxAllowed
        # rule no-ops. Previously this was a silent bypass; now invalid_type
        # surfaces the typo while the rule still skips (treated as missing).
        with pytest.raises(ConfigValidationError) as exc:
            validate_config(
                'vpa:\n  enabled: "true"\n'
                "resources:\n"
                "  requests: {cpu: 3, memory: 500Mi}\n"
                "  limits:   {cpu: 3, memory: 1Gi}\n"
            )
        rules = {v.rule for v in exc.value.violations}
        assert "invalid_type" in rules
        assert "requests_exceeds_vpa_max_cpu" not in rules

    def test_real_bool_passes_type_check(self):
        # Sanity: explicit bool false on split-enabled is the existing twc rule,
        # not invalid_type.
        with pytest.raises(ConfigValidationError) as exc:
            validate_config(
                "splitDeploymentEnabled: true\n"
                "temporalWorkerDeployment:\n  enabled: false\n"
            )
        rules = {v.rule for v in exc.value.violations}
        assert "twc_required_for_split" in rules
        assert "invalid_type" not in rules


class TestTwcSdkFloor:
    def test_twd_enabled_on_old_sdk_fails(self):
        # SDK < 2.7.4 has no TWC controller. App owner setting enabled=true
        # is a silent no-op in cluster — chart drops the block. Must fail
        # at config time.
        with pytest.raises(ConfigValidationError) as exc:
            validate_config(
                "temporalWorkerDeployment:\n  enabled: true\n",
                sdk_version="2.6.0",
            )
        rules = {v.rule for v in exc.value.violations}
        assert "twc_requires_sdk_2_7_4" in rules

    def test_twd_enabled_at_floor_passes(self):
        validate_config(
            "temporalWorkerDeployment:\n  enabled: true\n",
            sdk_version="2.7.4",
        )

    def test_twd_enabled_above_floor_passes(self):
        validate_config(
            "temporalWorkerDeployment:\n  enabled: true\n",
            sdk_version="3.6.0",
        )

    def test_twd_disabled_on_old_sdk_passes(self):
        # Explicit enabled=false on old SDK is harmless — chart ignores block.
        validate_config(
            "temporalWorkerDeployment:\n  enabled: false\n",
            sdk_version="2.6.0",
        )

    def test_split_disabled_check_skipped_below_floor(self):
        # On SDK < 2.7.4, the split + twd.enabled=false rule must NOT fire —
        # user has no way to enable TWC there. Only the enabled=true case
        # is caught (by _check_twc_sdk_floor).
        validate_config(
            "splitDeploymentEnabled: true\n"
            "temporalWorkerDeployment:\n  enabled: false\n",
            sdk_version="2.6.0",
        )

    def test_split_disabled_check_fires_at_or_above_floor(self):
        with pytest.raises(ConfigValidationError) as exc:
            validate_config(
                "splitDeploymentEnabled: true\n"
                "temporalWorkerDeployment:\n  enabled: false\n",
                sdk_version="2.7.4",
            )
        rules = {v.rule for v in exc.value.violations}
        assert "twc_required_for_split" in rules

    def test_no_sdk_version_skips_floor_rule(self):
        # sdk_version=None (e.g. atlan.yaml without sdk_version block) → can't
        # gate. Skip floor rule rather than guess.
        validate_config("temporalWorkerDeployment:\n  enabled: true\n")

    def test_invalid_sdk_version_skips_floor_rule(self):
        # Driver fails loud on invalid sdk_version before reaching here; if
        # the dict-input test path bypasses the driver, treat as unknown.
        validate_config(
            "temporalWorkerDeployment:\n  enabled: true\n",
            sdk_version="not-a-version",
        )

    def test_twc_required_message_has_no_version(self):
        # Regression: twc_required_for_split message must NOT name a specific
        # SDK version — that constraint lives in twc_requires_sdk_2_7_4 now.
        with pytest.raises(ConfigValidationError) as exc:
            validate_config(
                "splitDeploymentEnabled: true\n"
                "temporalWorkerDeployment:\n  enabled: false\n",
                sdk_version="2.7.4",
            )
        twc = next(
            v for v in exc.value.violations if v.rule == "twc_required_for_split"
        )
        assert "2.7.4" not in twc.fix
        assert "2.7.4" not in str(twc.expected)

    def test_floor_message_names_version(self):
        # Inverse: twc_requires_sdk_2_7_4 MUST name the version — that's the
        # whole point of the rule.
        with pytest.raises(ConfigValidationError) as exc:
            validate_config(
                "temporalWorkerDeployment:\n  enabled: true\n",
                sdk_version="2.6.0",
            )
        floor = next(
            v for v in exc.value.violations if v.rule == "twc_requires_sdk_2_7_4"
        )
        assert "2.7.4" in floor.fix
