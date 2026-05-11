"""
Validate atlan.yaml deploy config against platform guardrails in CI.

Rules enforced:
  1. splitDeploymentEnabled=true + temporalWorkerDeployment.enabled=false fails
     (image-pull / crashloop only surfaces via TWC during version rollout).
  1b. temporalWorkerDeployment.enabled=true on SDK < 2.7.4 fails
      (TWC is unsupported below 2.7.4 — chart silently ignores the block).
  2. vpa.maxAllowed.cpu     <= 7 cores (7000m).
  3. vpa.maxAllowed.memory  <= 27Gi (binary, = 27 * 1024^3 bytes).
  4. requests.cpu           <= 7 cores.
  5. requests.memory        <= 27Gi.
  6. When vpa.enabled=true: requests.{cpu,memory} <= effective vpa.maxAllowed
     (chart defaults cpu=2000m, memory=18Gi when not declared).
  7. requests <= limits per resource.
  8. vpa.minAllowed         <= vpa.maxAllowed per resource.
  9. keda.minReplicaCount   <= keda.maxReplicaCount.

Rules 4-7 hit `resources` always; `serverResources`/`workerResources` only when
`splitDeploymentEnabled=true` (chart ignores them otherwise).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

# TWC (TemporalWorkerDeployment) controller support landed in SDK 2.7.4.
# Apps below this version that set `temporalWorkerDeployment.enabled: true`
# get silently ignored by the chart — they think TWC is on, it isn't.
MIN_SDK_FOR_TWC: Version = Version("2.7.4")

# Hardcoded infra ceilings. Change requires PR + platform-team review.
MAX_VPA_CPU_MILLI: int = 7_000
MAX_VPA_MEMORY_BYTES: int = 27 * 1024**3

# Mirror chart-shipped defaults so validator's effective ceiling matches what
# VPA actually enforces in cluster.
DEFAULT_VPA_MAX_CPU_MILLI: int = 2_000
DEFAULT_VPA_MAX_MEMORY_BYTES: int = 18 * 1024**3

# Binary (Ki/Mi/Gi/...) use 1024; decimal (k/K/M/G/...) use 1000. Not interchangeable.
_MEM_SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}
_CPU_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(m)?\s*$")
_MEM_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMGTPE]i?|k)?\s*$")


@dataclass
class Violation:
    field: str
    actual: Any
    expected: Any
    rule: str
    fix: str

    def to_dict(self) -> dict:
        return asdict(self)


class ConfigValidationError(ValueError):
    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        body = "\n".join(
            f"- [{v.rule}] {v.field}={v.actual!r} (expected: {v.expected}). {v.fix}"
            for v in violations
        )
        super().__init__("atlan.yaml validation failed:\n" + body)


def parse_cpu(value: Any) -> int:
    """Parse Kubernetes CPU quantity into millicores. int/float treated as cores."""
    if isinstance(value, bool):  # bool subclass of int — guard explicitly
        raise ValueError(f"invalid cpu quantity: {value!r}")
    if isinstance(value, (int, float)):
        return int(round(float(value) * 1000))
    s = str(value)
    m = _CPU_RE.match(s)
    if not m:
        raise ValueError(f"invalid cpu quantity: {value!r}")
    num = float(m.group(1))
    return int(round(num)) if m.group(2) == "m" else int(round(num * 1000))


def parse_memory(value: Any) -> int:
    """Parse Kubernetes memory quantity into bytes. int/float treated as bytes."""
    if isinstance(value, bool):
        raise ValueError(f"invalid memory quantity: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value)
    m = _MEM_RE.match(s)
    if not m:
        raise ValueError(f"invalid memory quantity: {value!r}")
    num = float(m.group(1))
    suffix = m.group(2)
    mult = _MEM_SUFFIXES[suffix] if suffix else 1
    return int(round(num * mult))


def _safe_parse_cpu(value: Any, field: str, errs: list[Violation]) -> int | None:
    try:
        return parse_cpu(value)
    except ValueError as e:
        errs.append(
            Violation(
                field=field,
                actual=value,
                expected="valid CPU quantity (e.g. '100m', '1', '1.5')",
                rule="invalid_quantity",
                fix=str(e),
            )
        )
        return None


def _safe_parse_memory(value: Any, field: str, errs: list[Violation]) -> int | None:
    try:
        return parse_memory(value)
    except ValueError as e:
        errs.append(
            Violation(
                field=field,
                actual=value,
                expected="valid memory quantity (e.g. '500Mi', '1Gi')",
                rule="invalid_quantity",
                fix=str(e),
            )
        )
        return None


# Top-level / nested boolean flags. `is True` / `is False` identity checks
# silently bypass validation if a user writes a quoted string (e.g.
# `splitDeploymentEnabled: "true"`) — YAML loads it as `str`, not `bool`.
# _parse_bools type-checks these up front and emits invalid_type once.
_BOOL_FIELDS: tuple[str, ...] = (
    "splitDeploymentEnabled",
    "vpa.enabled",
    "temporalWorkerDeployment.enabled",
)


def _parse_bools(cfg: dict) -> tuple[dict[str, bool | None], list[Violation]]:
    """Type-check known boolean fields once. Returns (values, violations).

    Each field in *values* is True / False if explicitly set to bool, or None
    if missing or invalid (non-bool). Non-bool values emit one invalid_type
    violation per field; downstream rules then treat them as missing.
    """
    parsed: dict[str, bool | None] = {}
    errs: list[Violation] = []
    for path in _BOOL_FIELDS:
        parts = path.split(".")
        node: Any = cfg
        for p in parts[:-1]:
            node = node.get(p) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            parsed[path] = None
            continue
        leaf = parts[-1]
        if leaf not in node:
            parsed[path] = None
            continue
        val = node[leaf]
        if isinstance(val, bool):
            parsed[path] = val
            continue
        errs.append(
            Violation(
                field=path,
                actual=val,
                expected="boolean (unquoted true or false)",
                rule="invalid_type",
                fix=(
                    f"Set {path} to an unquoted true or false (got "
                    f'{type(val).__name__}). Quoted strings like "true" '
                    "are not booleans and silently disable validation."
                ),
            )
        )
        parsed[path] = None
    return parsed, errs


def _check_split_requires_twc(
    cfg: dict, bools: dict[str, bool | None], sdk_version: Version | None
) -> list[Violation]:
    """Fail only on explicit `temporalWorkerDeployment.enabled: false` under split.

    Missing field, missing `enabled` key, or `enabled: true` all pass. Skip
    entirely on SDK < 2.7.4 — TWC unsupported there, so explicit disable is
    a no-op (the enabled=true case is caught by _check_twc_sdk_floor).
    """
    if sdk_version is not None and sdk_version < MIN_SDK_FOR_TWC:
        return []
    if bools.get("splitDeploymentEnabled") is not True:
        return []
    if bools.get("temporalWorkerDeployment.enabled") is not False:
        return []
    return [
        Violation(
            field="temporalWorkerDeployment.enabled",
            actual=False,
            expected="true (or omit the temporalWorkerDeployment block)",
            rule="twc_required_for_split",
            fix=(
                "Set temporalWorkerDeployment.enabled: true, or remove the "
                "temporalWorkerDeployment block. Split-worker deployments "
                "must use TWC so image-pull and crashloop failures surface "
                "during version rollout."
            ),
        )
    ]


def _check_twc_sdk_floor(
    bools: dict[str, bool | None], sdk_version: Version | None
) -> list[Violation]:
    """Fail when `temporalWorkerDeployment.enabled: true` on SDK < 2.7.4.

    TWC controller support landed in 2.7.4. Older SDKs ship a chart that
    silently drops the temporalWorkerDeployment block — app owner thinks
    TWC is on, it isn't, and they discover it only when a bad image rolls
    out without crashloop detection. Skip when sdk_version unknown (driver
    already fails loud on InvalidVersion).
    """
    if sdk_version is None:
        return []
    if bools.get("temporalWorkerDeployment.enabled") is not True:
        return []
    if sdk_version >= MIN_SDK_FOR_TWC:
        return []
    return [
        Violation(
            field="temporalWorkerDeployment.enabled",
            actual=True,
            expected=f"unset (TWC requires application-sdk >= {MIN_SDK_FOR_TWC})",
            rule="twc_requires_sdk_2_7_4",
            fix=(
                f"Upgrade application-sdk to >= {MIN_SDK_FOR_TWC}, or remove "
                "the temporalWorkerDeployment block. TWC is unsupported on "
                "older SDKs and the chart silently ignores it — leaving "
                "image-pull and crashloop failures undetected at rollout."
            ),
        )
    ]


def _parse_vpa(
    cfg: dict,
) -> tuple[dict[tuple[str, str], int | None], list[Violation]]:
    """Parse vpa.{minAllowed,maxAllowed} once. Shared by _check_vpa and
    _resolve_effective_vpa_max to avoid duplicate invalid_quantity violations."""
    vpa = cfg.get("vpa") or {}
    mn = vpa.get("minAllowed") or {}
    mx = vpa.get("maxAllowed") or {}
    parsed: dict[tuple[str, str], int | None] = {}
    errs: list[Violation] = []
    for kind, src in (("minAllowed", mn), ("maxAllowed", mx)):
        for resource, parser in (
            ("cpu", _safe_parse_cpu),
            ("memory", _safe_parse_memory),
        ):
            if resource in src:
                parsed[(resource, kind)] = parser(
                    src[resource], f"vpa.{kind}.{resource}", errs
                )
    return parsed, errs


def _check_vpa(cfg: dict, parsed: dict[tuple[str, str], int | None]) -> list[Violation]:
    vpa = cfg.get("vpa") or {}
    mn = vpa.get("minAllowed") or {}
    mx = vpa.get("maxAllowed") or {}
    errs: list[Violation] = []

    cpu_max = parsed.get(("cpu", "maxAllowed"))
    if cpu_max is not None and cpu_max > MAX_VPA_CPU_MILLI:
        errs.append(
            Violation(
                field="vpa.maxAllowed.cpu",
                actual=mx.get("cpu"),
                expected=f"<= 7 cores ({MAX_VPA_CPU_MILLI}m)",
                rule="vpa_max_cpu_ceiling",
                fix="Lower vpa.maxAllowed.cpu to 7 cores or less.",
            )
        )

    mem_max = parsed.get(("memory", "maxAllowed"))
    if mem_max is not None and mem_max > MAX_VPA_MEMORY_BYTES:
        errs.append(
            Violation(
                field="vpa.maxAllowed.memory",
                actual=mx.get("memory"),
                expected="<= 27Gi",
                rule="vpa_max_memory_ceiling",
                fix="Lower vpa.maxAllowed.memory to 27Gi or less.",
            )
        )

    for resource in ("cpu", "memory"):
        a = parsed.get((resource, "minAllowed"))
        b = parsed.get((resource, "maxAllowed"))
        if a is None or b is None:
            continue
        if a > b:
            errs.append(
                Violation(
                    field=f"vpa.minAllowed.{resource}",
                    actual=mn[resource],
                    expected=f"<= vpa.maxAllowed.{resource} ({mx[resource]})",
                    rule="vpa_min_le_max",
                    fix=f"Lower vpa.minAllowed.{resource} or raise vpa.maxAllowed.{resource}.",
                )
            )
    return errs


def _resolve_effective_vpa_max(
    cfg: dict,
    parsed: dict[tuple[str, str], int | None],
    vpa_enabled: bool | None,
) -> tuple[int | None, int | None]:
    """Effective vpa.maxAllowed (cpu_milli, mem_bytes), or (None, None) when vpa disabled.
    Falls back to DEFAULT_VPA_MAX_* when maxAllowed not declared."""
    if vpa_enabled is not True:
        return None, None
    vpa = cfg.get("vpa") or {}
    max_allowed = vpa.get("maxAllowed") or {}
    cpu_milli: int | None = (
        parsed.get(("cpu", "maxAllowed"))
        if "cpu" in max_allowed
        else DEFAULT_VPA_MAX_CPU_MILLI
    )
    mem_bytes: int | None = (
        parsed.get(("memory", "maxAllowed"))
        if "memory" in max_allowed
        else DEFAULT_VPA_MAX_MEMORY_BYTES
    )
    return cpu_milli, mem_bytes


def _check_resource_block(
    cfg: dict,
    key: str,
    vpa_max_cpu_milli: int | None = None,
    vpa_max_memory_bytes: int | None = None,
) -> list[Violation]:
    """Validate resources / serverResources / workerResources.
    None vpa_max_* skips the requests<=vpa.maxAllowed rule (vpa disabled)."""
    block = cfg.get(key) or {}
    if not block:
        return []
    requests = block.get("requests") or {}
    limits = block.get("limits") or {}
    errs: list[Violation] = []

    # Parse-once cache: avoids duplicate invalid_quantity violations across
    # the multiple rules that touch the same field.
    parsed: dict[tuple[str, str], int | None] = {}
    for kind, src in (("requests", requests), ("limits", limits)):
        for resource, parser in (
            ("cpu", _safe_parse_cpu),
            ("memory", _safe_parse_memory),
        ):
            if resource in src:
                parsed[(resource, kind)] = parser(
                    src[resource], f"{key}.{kind}.{resource}", errs
                )

    # Even without VPA, raw request above infra guarantee fails to schedule.
    cpu_req = parsed.get(("cpu", "requests"))
    if cpu_req is not None and cpu_req > MAX_VPA_CPU_MILLI:
        errs.append(
            Violation(
                field=f"{key}.requests.cpu",
                actual=requests.get("cpu"),
                expected=f"<= 7 cores ({MAX_VPA_CPU_MILLI}m)",
                rule="requests_cpu_ceiling",
                fix=f"Lower {key}.requests.cpu to 7 cores or less.",
            )
        )
    mem_req = parsed.get(("memory", "requests"))
    if mem_req is not None and mem_req > MAX_VPA_MEMORY_BYTES:
        errs.append(
            Violation(
                field=f"{key}.requests.memory",
                actual=requests.get("memory"),
                expected="<= 27Gi",
                rule="requests_memory_ceiling",
                fix=f"Lower {key}.requests.memory to 27Gi or less.",
            )
        )

    # Initial request above vpa.maxAllowed gets clamped down by VPA admission,
    # surprising the app owner — fail at config time instead.
    if (
        cpu_req is not None
        and vpa_max_cpu_milli is not None
        and cpu_req > vpa_max_cpu_milli
    ):
        errs.append(
            Violation(
                field=f"{key}.requests.cpu",
                actual=requests.get("cpu"),
                expected=f"<= vpa.maxAllowed.cpu ({vpa_max_cpu_milli}m)",
                rule="requests_exceeds_vpa_max_cpu",
                fix=f"Lower {key}.requests.cpu, raise vpa.maxAllowed.cpu, or disable vpa.enabled.",
            )
        )
    if (
        mem_req is not None
        and vpa_max_memory_bytes is not None
        and mem_req > vpa_max_memory_bytes
    ):
        errs.append(
            Violation(
                field=f"{key}.requests.memory",
                actual=requests.get("memory"),
                expected=f"<= vpa.maxAllowed.memory ({vpa_max_memory_bytes} bytes)",
                rule="requests_exceeds_vpa_max_memory",
                fix=f"Lower {key}.requests.memory, raise vpa.maxAllowed.memory, or disable vpa.enabled.",
            )
        )

    for resource in ("cpu", "memory"):
        if resource not in requests or resource not in limits:
            continue
        req = parsed.get((resource, "requests"))
        lim = parsed.get((resource, "limits"))
        if req is None or lim is None:
            continue
        if req > lim:
            errs.append(
                Violation(
                    field=f"{key}.requests.{resource}",
                    actual=requests[resource],
                    expected=f"<= {key}.limits.{resource} ({limits[resource]})",
                    rule="requests_le_limits",
                    fix=f"Ensure {key}.requests.{resource} is not greater than {key}.limits.{resource}.",
                )
            )

    return errs


def _check_resources(
    cfg: dict,
    vpa_parsed: dict[tuple[str, str], int | None],
    bools: dict[str, bool | None],
) -> list[Violation]:
    vpa_cpu, vpa_mem = _resolve_effective_vpa_max(
        cfg, vpa_parsed, bools.get("vpa.enabled")
    )
    errs = _check_resource_block(cfg, "resources", vpa_cpu, vpa_mem)
    if bools.get("splitDeploymentEnabled") is True:
        for k in ("serverResources", "workerResources"):
            errs += _check_resource_block(cfg, k, vpa_cpu, vpa_mem)
    return errs


def _check_keda(cfg: dict) -> list[Violation]:
    """keda.minReplicaCount <= keda.maxReplicaCount (when both set).

    Non-int / bool replica counts silently skip — chart schema handles type errors.
    """
    keda = cfg.get("keda") or {}
    mn = keda.get("minReplicaCount")
    mx = keda.get("maxReplicaCount")
    if mn is None or mx is None:
        return []
    if isinstance(mn, bool) or isinstance(mx, bool):  # bool subclass of int
        return []
    if not isinstance(mn, int) or not isinstance(mx, int):
        return []
    if mn > mx:
        return [
            Violation(
                field="keda.minReplicaCount",
                actual=mn,
                expected=f"<= keda.maxReplicaCount ({mx})",
                rule="keda_min_le_max",
                fix="Lower keda.minReplicaCount or raise keda.maxReplicaCount.",
            )
        ]
    return []


def validate_config(config_yaml: Any, sdk_version: str | None = None) -> None:
    """Run all guardrail rules. Accepts YAML string or already-parsed dict.

    *sdk_version* (optional) gates version-coupled rules — currently the TWC
    floor at 2.7.4. Pass None to skip those rules (driver fails loud on
    InvalidVersion before reaching here, so None means "not provided").

    Raises ConfigValidationError with aggregated violations from a single
    pass. No-op on non-mapping input.
    """
    if isinstance(config_yaml, dict):
        cfg = config_yaml
    else:
        try:
            cfg = yaml.safe_load(config_yaml or "") or {}
        except yaml.YAMLError as e:
            raise ConfigValidationError(
                [
                    Violation(
                        field="<yaml>",
                        actual=str(e),
                        expected="valid YAML",
                        rule="yaml_parse",
                        fix="Fix YAML syntax in atlan.yaml.",
                    )
                ]
            )

    if not isinstance(cfg, dict):
        return

    parsed_sdk: Version | None = None
    if sdk_version:
        try:
            parsed_sdk = Version(sdk_version)
        except InvalidVersion:
            parsed_sdk = None

    errs: list[Violation] = []
    bools, bool_errs = _parse_bools(cfg)
    errs += bool_errs
    vpa_parsed, vpa_parse_errs = _parse_vpa(cfg)
    errs += vpa_parse_errs
    errs += _check_split_requires_twc(cfg, bools, parsed_sdk)
    errs += _check_twc_sdk_floor(bools, parsed_sdk)
    errs += _check_vpa(cfg, vpa_parsed)
    errs += _check_resources(cfg, vpa_parsed, bools)
    errs += _check_keda(cfg)

    if errs:
        raise ConfigValidationError(errs)
