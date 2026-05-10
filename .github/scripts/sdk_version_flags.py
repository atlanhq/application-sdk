"""
Inject version-gated Helm flags into app config YAML based on application-sdk version.

Apps built on application-sdk gain capabilities at specific versions (e.g. split
deployment at 2.3.1, Temporal Worker Deployment at 2.7.4). Rather than requiring
every app owner to manually add these flags to their atlan.yaml, this module
injects them automatically at publish time.

Two flag classes:

  - **Additive** (default): inject default_value only when the top-level key
    is absent from the app's config. App owners' explicit values are
    preserved — they can opt out by setting the flag to false or customise
    sub-fields like keda.minReplicaCount.

  - **Force-override** (`force_override=True`): inject default_value
    unconditionally, replacing any explicit value the app set. Used to
    retire deprecated flags at a specific SDK cutover — e.g. SDK ≥ 3.6.0
    apps must not set `customMetrics` or `temporalMetrics`, since the
    chart now handles both surfaces via the unified `metrics.enabled` flag.
"""

import logging
from typing import Any, List, Optional, Tuple

import yaml
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

# (minimum_sdk_version, top_level_key, default_value, maximum_sdk_version, force_override)
# default_value is applied when:
#   - sdk_version >= minimum_sdk_version
#   - sdk_version <  maximum_sdk_version  (None means no upper bound)
#   - the top-level key is absent from config (or force_override=True)
# Version objects are pre-computed at module load to avoid re-parsing on every call.
VERSION_GATED_FLAGS: List[Tuple[Version, str, Any, Optional[Version], bool]] = [
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
    """Parse a PEP 440 version string, returning None on failure."""
    try:
        return Version(version_str)
    except InvalidVersion:
        logger.warning(
            f"Cannot parse sdk_version '{version_str}', skipping flag injection"
        )
        return None


def inject_sdk_version_flags(config_yaml: str, sdk_version: str | None) -> str:
    """
    Enrich *config_yaml* with version-gated flags based on *sdk_version*.

    Rules:
    - If sdk_version is None/empty/unparseable, return config_yaml unchanged.
    - For each flag whose minimum version is satisfied:
      - If the **top-level key** already exists in config, skip it entirely
        (preserves any explicit value or sub-structure the app owner set).
      - Otherwise, inject the default value.
    - Returns the (possibly enriched) YAML string.
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
        # max_version is exclusive: lets us retire a flag at a specific SDK
        # version (e.g. temporalMetrics is replaced by `metrics` in 3.6.0).
        if max_version is not None and parsed_version >= max_version:
            continue

        if key in config:
            # Force-override classes: replace user value if it differs.
            # Otherwise (additive class): preserve user's explicit value.
            if force_override and config[key] != default_value:
                overrides[key] = default_value
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
        # Force-overrides require mutating existing keys, so we must re-emit
        # the whole config. This produces a cosmetic diff on release-card
        # comparisons, but force-override is intentionally rare (deprecated
        # flag retirement) and the diff is meaningful — the app owner needs
        # to know their explicit value was replaced.
        merged = dict(config)
        merged.update(overrides)
        merged.update(additions)
        return yaml.dump(merged, default_flow_style=False, sort_keys=False)

    # Pure additions: append as a YAML block instead of re-dumping the whole
    # config. Why: yaml.dump rewrites indentation, quote style, and list-item
    # layout, which surfaces as a noisy cosmetic diff on release-card version
    # comparisons even though the app owner's atlan.yaml never changed.
    addendum = yaml.dump(additions, default_flow_style=False, sort_keys=False)
    base = config_yaml or ""
    if base and not base.endswith("\n"):
        base += "\n"
    return base + addendum
