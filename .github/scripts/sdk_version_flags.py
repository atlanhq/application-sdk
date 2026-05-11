"""
Inject version-gated Helm flags into app config YAML based on application-sdk version.

Two flag classes:
  - **Additive** (default): inject default only when key absent. Preserves
    app owner's explicit values.
  - **Force-override** (`force_override=True`): inject unconditionally. Used
    to retire deprecated flags at a specific SDK cutover.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

# (min_version, key, default_value, max_version, force_override)
# Applied when min_version <= sdk_version < max_version (max=None means no upper bound)
# AND (key absent OR force_override=True).
VERSION_GATED_FLAGS: list[tuple[Version, str, Any, Version | None, bool]] = [
    (Version("2.3.1"), "splitDeploymentEnabled", True, None, False),
    (Version("2.3.1"), "vpa", {"enabled": True}, None, False),
    (Version("2.5.0"), "keda", {"enabled": True, "minReplicaCount": 0}, None, False),
    (Version("2.5.0"), "temporalMetrics", {"enabled": True}, Version("3.6.0"), False),
    (Version("2.7.4"), "temporalWorkerDeployment", {"enabled": True}, None, False),
    (Version("3.6.0"), "metrics", {"enabled": True}, None, False),
    (Version("3.6.0"), "customMetrics", {"enabled": False}, None, True),
    (Version("3.6.0"), "temporalMetrics", {"enabled": False}, None, True),
]


def _parse_version(version_str: str) -> Version | None:
    try:
        return Version(version_str)
    except InvalidVersion:
        logger.warning(
            f"Cannot parse sdk_version '{version_str}', skipping flag injection"
        )
        return None


def inject_sdk_version_flags(config_yaml: str, sdk_version: str | None) -> str:
    """Enrich config_yaml with version-gated flags. Returns YAML string.
    No-op when sdk_version is None/empty/unparseable or YAML is unparseable.
    """
    if not sdk_version or not sdk_version.strip():
        return config_yaml

    parsed_version = _parse_version(sdk_version.strip())
    if parsed_version is None:
        return config_yaml

    try:
        config = yaml.safe_load(config_yaml or "") or {}
    except yaml.YAMLError:
        logger.warning(
            "Failed to parse config YAML for flag injection, returning as-is"
        )
        return config_yaml

    if not isinstance(config, dict):
        return config_yaml

    additions: dict[str, Any] = {}
    overrides: dict[str, Any] = {}
    for (
        min_version,
        key,
        default_value,
        max_version,
        force_override,
    ) in VERSION_GATED_FLAGS:
        if parsed_version < min_version:
            continue
        # max_version is exclusive — retires a flag at a specific SDK cutover.
        if max_version is not None and parsed_version >= max_version:
            continue

        if key in config:
            if force_override and config[key] != default_value:
                overrides[key] = default_value
                existing = config[key]
                # Force-override of a dict replaces the entire value: any
                # sibling sub-keys the app owner set (e.g. customMetrics.interval,
                # customMetrics.labels) are silently dropped. Surface them at
                # warn-level so the CI log shows what's lost.
                if isinstance(existing, dict) and isinstance(default_value, dict):
                    dropped = sorted(set(existing) - set(default_value))
                    if dropped:
                        logger.warning(
                            f"Force-override of {key} drops sibling sub-keys "
                            f"{dropped} for sdk_version={sdk_version}. These "
                            "are no longer honoured by the chart; remove them "
                            "from atlan.yaml."
                        )
                logger.info(
                    f"Force-overriding {key}={config[key]!r} → {default_value!r} "
                    f"for sdk_version={sdk_version}"
                )
            continue

        additions[key] = default_value
        logger.info(f"Injected {key}={default_value} for sdk_version={sdk_version}")

    if not additions and not overrides:
        return config_yaml

    if overrides:
        # Force-override mutates existing keys → must re-emit whole config.
        # Produces cosmetic diff on release-card comparisons, but the diff is
        # meaningful — app owner needs to see their explicit value replaced.
        merged = dict(config)
        merged.update(overrides)
        merged.update(additions)
        return yaml.dump(merged, default_flow_style=False, sort_keys=False)

    # Pure-additions path appends YAML block instead of re-dumping the whole
    # config to avoid noisy cosmetic diffs from yaml.dump's reformatting.
    addendum = yaml.dump(additions, default_flow_style=False, sort_keys=False)
    base = config_yaml or ""
    if base and not base.endswith("\n"):
        base += "\n"
    appended = base + addendum

    # Defensive re-parse: raw append breaks if base ends mid-list/mid-mapping.
    # On parse failure or missing top-level keys, fall back to dict-merge re-emit.
    try:
        appended_parsed = yaml.safe_load(appended)
        if isinstance(appended_parsed, dict):
            expected_keys = set(config) | set(additions)
            if set(appended_parsed) >= expected_keys:
                return appended
    except yaml.YAMLError:
        pass

    logger.info(
        "Pure-additions append corrupted YAML structure for sdk_version=%s, "
        "falling back to dict-merge + re-emit",
        sdk_version,
    )
    merged = dict(config)
    merged.update(additions)
    return yaml.dump(merged, default_flow_style=False, sort_keys=False)
