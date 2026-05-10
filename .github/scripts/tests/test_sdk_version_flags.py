"""Tests for .github/scripts/sdk_version_flags.py."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from sdk_version_flags import inject_sdk_version_flags


class TestAppendFallback:
    """Pure-additions append fallback safety.

    When the appended result fails to parse (or loses keys), the function
    must fall back to dict-merge + re-emit instead of returning corrupted YAML.
    """

    def test_append_preserves_valid_config(self):
        # Healthy top-level YAML — append path is taken, prefix preserved.
        original = "replicaCount: 1\n"
        result = inject_sdk_version_flags(original, "2.3.1")
        assert result.startswith(original), "append path expected for healthy YAML"
        assert yaml.safe_load(result)["replicaCount"] == 1
        assert yaml.safe_load(result)["splitDeploymentEnabled"] is True

    def test_append_fallback_on_corrupted_indent(self):
        # Original ends with a partially-indented block that would corrupt
        # if a top-level key were appended after it. The defensive re-parse
        # detects the corruption and the function re-emits the merged dict.
        original = "env:\n  FOO: bar"
        result = inject_sdk_version_flags(original, "2.3.1")
        # Whatever path was taken, the result must parse cleanly and contain
        # both the original env mapping and the injected flags.
        parsed = yaml.safe_load(result)
        assert parsed["env"]["FOO"] == "bar"
        assert parsed["splitDeploymentEnabled"] is True
        assert parsed["vpa"]["enabled"] is True


class TestInjectSdkVersionFlags:
    # ---- No-op cases ----

    def test_none_sdk_version_returns_unchanged(self):
        config = "replicaCount: 1"
        assert inject_sdk_version_flags(config, None) == config

    def test_empty_sdk_version_returns_unchanged(self):
        config = "replicaCount: 1"
        assert inject_sdk_version_flags(config, "") == config

    def test_unparseable_sdk_version_returns_unchanged(self):
        config = "replicaCount: 1"
        assert inject_sdk_version_flags(config, "not-a-version") == config

    def test_invalid_yaml_returns_unchanged(self):
        bad = "invalid: yaml: ["
        assert inject_sdk_version_flags(bad, "2.8.0") == bad

    # ---- Version below all thresholds ----

    def test_version_below_all_thresholds_no_injection(self):
        config = "replicaCount: 2"
        for version in ["2.2.0", "2.3.0"]:
            result = inject_sdk_version_flags(config, version)
            parsed = yaml.safe_load(result)
            assert "splitDeploymentEnabled" not in parsed
            assert "vpa" not in parsed
            assert "keda" not in parsed
            assert "temporalWorkerDeployment" not in parsed

    # ---- Version >= 2.3.1 ----

    def test_version_2_3_1_injects_split_vpa_only(self):
        config = "replicaCount: 2"
        result = inject_sdk_version_flags(config, "2.3.1")
        parsed = yaml.safe_load(result)
        assert parsed["splitDeploymentEnabled"] is True
        assert parsed["vpa"] == {"enabled": True}
        assert "keda" not in parsed
        assert "temporalMetrics" not in parsed
        assert "temporalWorkerDeployment" not in parsed

    # ---- Version >= 2.5.0 ----

    def test_version_2_5_0_injects_split_vpa_keda_temporal_metrics_not_twd(self):
        config = "replicaCount: 1"
        result = inject_sdk_version_flags(config, "2.5.0")
        parsed = yaml.safe_load(result)
        assert parsed["splitDeploymentEnabled"] is True
        assert parsed["vpa"] == {"enabled": True}
        assert parsed["keda"] == {"enabled": True, "minReplicaCount": 0}
        assert parsed["temporalMetrics"] == {"enabled": True}
        assert "temporalWorkerDeployment" not in parsed

    # ---- Version >= 2.7.4 ----

    def test_version_2_7_4_injects_all_flags(self):
        config = "replicaCount: 1"
        result = inject_sdk_version_flags(config, "2.7.4")
        parsed = yaml.safe_load(result)
        assert parsed["splitDeploymentEnabled"] is True
        assert parsed["vpa"] == {"enabled": True}
        assert parsed["keda"] == {"enabled": True, "minReplicaCount": 0}
        assert parsed["temporalMetrics"] == {"enabled": True}
        assert parsed["temporalWorkerDeployment"] == {"enabled": True}

    def test_version_3_6_0_swaps_temporal_metrics_for_unified_metrics_flag(self):
        """SDK ≥ 3.6.0 proxies Temporal core through FastAPI /metrics, so the
        separate `temporalMetrics` scrape is replaced by the unified
        `metrics.enabled` flag. 3.6.0+ apps should see `metrics` injected,
        and the deprecated `temporalMetrics`/`customMetrics` force-disabled
        (set to {enabled: false} explicitly)."""
        config = "replicaCount: 1"
        result = inject_sdk_version_flags(config, "3.6.0")
        parsed = yaml.safe_load(result)
        assert parsed["splitDeploymentEnabled"] is True
        assert parsed["vpa"] == {"enabled": True}
        assert parsed["keda"] == {"enabled": True, "minReplicaCount": 0}
        assert parsed["temporalWorkerDeployment"] == {"enabled": True}
        assert parsed["metrics"] == {"enabled": True}
        # Force-disabled — even when not previously set
        assert parsed["temporalMetrics"] == {"enabled": False}
        assert parsed["customMetrics"] == {"enabled": False}

    def test_version_3_6_0_force_disables_explicit_temporal_metrics(self):
        """If an app on SDK ≥ 3.6.0 has explicitly set temporalMetrics to
        true (e.g. copy-paste from an older example), it MUST be forced to
        false — otherwise the chart renders a phantom :9464 scrape against
        the loopback-only Temporal core port."""
        config = "temporalMetrics:\n  enabled: true\n"
        result = inject_sdk_version_flags(config, "3.6.0")
        parsed = yaml.safe_load(result)
        assert parsed["temporalMetrics"] == {"enabled": False}
        assert parsed["metrics"] == {"enabled": True}

    def test_version_3_6_0_force_disables_explicit_custom_metrics(self):
        """If an app on SDK ≥ 3.6.0 has explicitly set customMetrics, it MUST
        be forced to false. The unified `metrics.enabled` covers the same
        scrape surface; the deprecated key shouldn't be set."""
        config = "customMetrics:\n  enabled: true\n  interval: 30s\n"
        result = inject_sdk_version_flags(config, "3.6.0")
        parsed = yaml.safe_load(result)
        assert parsed["customMetrics"] == {"enabled": False}
        assert parsed["metrics"] == {"enabled": True}

    def test_version_below_3_6_0_preserves_explicit_temporal_metrics_true(self):
        """For SDK < 3.6.0, temporalMetrics is still the legacy path, so an
        app's explicit `temporalMetrics: enabled: true` MUST be preserved
        (no force-override below the cutover)."""
        config = "temporalMetrics:\n  enabled: true\n"
        result = inject_sdk_version_flags(config, "3.5.0")
        parsed = yaml.safe_load(result)
        assert parsed["temporalMetrics"] == {"enabled": True}

    def test_version_just_below_3_6_0_still_gets_temporal_metrics(self):
        """Boundary: 3.5.99 should still inject the legacy temporalMetrics
        flag. The cutover is exactly at 3.6.0 (exclusive max-version)."""
        result = inject_sdk_version_flags("replicaCount: 1", "3.5.99")
        parsed = yaml.safe_load(result)
        assert parsed["temporalMetrics"] == {"enabled": True}
        assert "metrics" not in parsed

    def test_versions_below_3_6_0_do_not_get_unified_metrics_flag(self):
        """All SDK versions <3.6.0 (including current 3.x apps) keep the
        legacy temporalMetrics auto-injection. The cutover at 3.6.0
        preserves behavior for currently-deployed v3 apps until they
        explicitly bump their SDK pin."""
        for version in ["2.5.0", "2.7.4", "2.8.0", "3.0.0", "3.4.1", "3.5.0"]:
            result = inject_sdk_version_flags("replicaCount: 1", version)
            parsed = yaml.safe_load(result)
            assert "metrics" not in parsed, f"version {version} should NOT get metrics"
            assert parsed["temporalMetrics"] == {
                "enabled": True
            }, f"version {version} should still get temporalMetrics"

    # ---- App owner overrides preserved ----

    def test_explicit_split_deployment_false_not_overridden(self):
        config = "splitDeploymentEnabled: false"
        result = inject_sdk_version_flags(config, "2.8.0")
        parsed = yaml.safe_load(result)
        assert parsed["splitDeploymentEnabled"] is False

    def test_explicit_vpa_custom_not_overridden(self):
        config = "vpa:\n  enabled: false\n  updateMode: 'Off'"
        result = inject_sdk_version_flags(config, "2.8.0")
        parsed = yaml.safe_load(result)
        assert parsed["vpa"]["enabled"] is False
        assert parsed["vpa"]["updateMode"] == "Off"

    def test_explicit_keda_custom_not_overridden(self):
        config = "keda:\n  enabled: true\n  minReplicaCount: 5\n  maxReplicaCount: 10"
        result = inject_sdk_version_flags(config, "2.8.0")
        parsed = yaml.safe_load(result)
        assert parsed["keda"]["minReplicaCount"] == 5
        assert parsed["keda"]["maxReplicaCount"] == 10

    def test_explicit_twd_false_not_overridden(self):
        config = "temporalWorkerDeployment:\n  enabled: false"
        result = inject_sdk_version_flags(config, "2.8.0")
        parsed = yaml.safe_load(result)
        assert parsed["temporalWorkerDeployment"]["enabled"] is False

    def test_bare_false_overrides_preserved(self):
        config = "keda: false\nvpa: false\ntemporalWorkerDeployment: false"
        result = inject_sdk_version_flags(config, "2.8.0")
        parsed = yaml.safe_load(result)
        assert parsed["keda"] is False
        assert parsed["vpa"] is False
        assert parsed["temporalWorkerDeployment"] is False

    # ---- Partial overrides ----

    def test_partial_override_injects_missing_only(self):
        config = "replicaCount: 2\nkeda:\n  enabled: true\n  minReplicaCount: 0"
        result = inject_sdk_version_flags(config, "2.8.0")
        parsed = yaml.safe_load(result)
        assert parsed["keda"]["minReplicaCount"] == 0
        assert parsed["splitDeploymentEnabled"] is True
        assert parsed["vpa"] == {"enabled": True}
        assert parsed["temporalMetrics"] == {"enabled": True}
        assert parsed["temporalWorkerDeployment"] == {"enabled": True}

    # ---- Existing config preserved ----

    def test_existing_config_fields_preserved(self):
        config = "replicaCount: 3\ncontainerPort: 9000\nenv:\n  MY_VAR: hello"
        result = inject_sdk_version_flags(config, "2.3.1")
        parsed = yaml.safe_load(result)
        assert parsed["replicaCount"] == 3
        assert parsed["containerPort"] == 9000
        assert parsed["env"]["MY_VAR"] == "hello"
        assert parsed["splitDeploymentEnabled"] is True
        assert "keda" not in parsed

    # ---- Edge cases ----

    def test_empty_config_with_valid_version(self):
        result = inject_sdk_version_flags("", "2.8.0")
        parsed = yaml.safe_load(result)
        assert parsed["splitDeploymentEnabled"] is True
        assert parsed["temporalMetrics"] == {"enabled": True}
        assert parsed["temporalWorkerDeployment"] == {"enabled": True}

    def test_whitespace_in_version_is_stripped(self):
        result = inject_sdk_version_flags("replicaCount: 1", "  2.8.0  ")
        parsed = yaml.safe_load(result)
        assert parsed["splitDeploymentEnabled"] is True

    def test_prerelease_version_satisfies_threshold(self):
        result = inject_sdk_version_flags("replicaCount: 1", "2.8.0rc1")
        parsed = yaml.safe_load(result)
        assert parsed["splitDeploymentEnabled"] is True

    def test_prerelease_version_below_threshold(self):
        """2.3.1rc1 is LESS than 2.3.1 per PEP 440."""
        result = inject_sdk_version_flags("replicaCount: 1", "2.3.1rc1")
        parsed = yaml.safe_load(result)
        assert "splitDeploymentEnabled" not in parsed

    def test_non_dict_yaml_returns_unchanged(self):
        """Scalars and lists should pass through untouched."""
        for config in ["42", "true", "- item1\n- item2"]:
            assert inject_sdk_version_flags(config, "2.8.0") == config

    def test_bare_false_does_not_inject_subfields(self):
        """keda: false should NOT inject minReplicaCount."""
        config = "keda: false\nvpa: false\ntemporalWorkerDeployment: false"
        result = inject_sdk_version_flags(config, "2.8.0")
        parsed = yaml.safe_load(result)
        assert parsed["keda"] is False
        assert parsed["vpa"] is False
        assert parsed["temporalWorkerDeployment"] is False

    def test_setting_one_key_does_not_skip_others(self):
        """Each flag is independent -- setting keda should not skip vpa/split/temporalMetrics/twd."""
        config = "keda:\n  enabled: true\n  minReplicaCount: 5"
        result = inject_sdk_version_flags(config, "2.8.0")
        parsed = yaml.safe_load(result)
        assert parsed["keda"] == {"enabled": True, "minReplicaCount": 5}
        assert parsed["splitDeploymentEnabled"] is True
        assert parsed["vpa"] == {"enabled": True}
        assert parsed["temporalMetrics"] == {"enabled": True}
        assert parsed["temporalWorkerDeployment"] == {"enabled": True}

    def test_version_just_below_twd_threshold(self):
        """2.7.3 should get split/vpa/keda/temporalMetrics but NOT temporalWorkerDeployment."""
        result = inject_sdk_version_flags("replicaCount: 1", "2.7.3")
        parsed = yaml.safe_load(result)
        assert parsed["splitDeploymentEnabled"] is True
        assert parsed["vpa"] == {"enabled": True}
        assert parsed["keda"] == {"enabled": True, "minReplicaCount": 0}
        assert parsed["temporalMetrics"] == {"enabled": True}
        assert "temporalWorkerDeployment" not in parsed

    # ---- Original formatting preserved (release-card diff regression) ----

    def test_original_formatting_preserved_when_injecting(self):
        """Existing YAML text must be byte-for-byte preserved; only new keys are appended.

        Regression: yaml.dump round-tripping the whole config rewrote indentation,
        quote style, and list-item layout, producing noisy cosmetic diffs on the
        release-card version comparison even when the app owner's atlan.yaml had
        not changed.
        """
        original = (
            "env:\n"
            '    ATLAN_HEARTBEAT_TIMEOUT_SECONDS: "120"\n'
            "envFrom:\n"
            "    secrets:\n"
            "        - keys:\n"
            "            ATLAN_SEGMENT_WRITE_KEY: SEGMENT_WRITE_KEY\n"
            "          name: segment-write-key\n"
        )
        result = inject_sdk_version_flags(original, "2.8.0")
        assert result.startswith(
            original
        ), "original YAML must be preserved verbatim as a prefix"
        parsed = yaml.safe_load(result)
        assert parsed["env"]["ATLAN_HEARTBEAT_TIMEOUT_SECONDS"] == "120"
        assert parsed["splitDeploymentEnabled"] is True

    def test_no_injection_returns_input_identity(self):
        """When no flags are injected, the exact input string is returned."""
        original = 'env:\n    FOO: "bar"\n'
        result = inject_sdk_version_flags(original, "2.3.1")  # only injects split/vpa
        # split & vpa get appended; original prefix preserved
        assert result.startswith(original)

        # Now provide a config that already has every gated key for SDK 3.6.0
        # (the cutover where temporalMetrics is replaced by `metrics`).
        # customMetrics/temporalMetrics force-override defaults must already
        # match — otherwise the override path triggers and the config is
        # re-emitted (not identity).
        full = (
            "splitDeploymentEnabled: true\n"
            "vpa:\n    enabled: true\n"
            "keda:\n    enabled: true\n"
            "metrics:\n    enabled: true\n"
            "temporalWorkerDeployment:\n    enabled: true\n"
            "customMetrics:\n    enabled: false\n"
            "temporalMetrics:\n    enabled: false\n"
        )
        assert inject_sdk_version_flags(full, "3.6.0") is full
