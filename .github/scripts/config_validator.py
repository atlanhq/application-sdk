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
  6. When vpa.enabled=true AND updateMode != "Off": requests.{cpu,memory}
     <= effective vpa.maxAllowed (chart defaults cpu=2000m, memory=18Gi
     when not declared). Skipped in updateMode=Off — VPA only emits
     recommendations there and doesn't clamp admission.
  7. requests <= limits per resource.
  8. vpa.minAllowed         <= vpa.maxAllowed per resource.
  9. keda.minReplicaCount   <= keda.maxReplicaCount.
 10. Scalar fields the chart schema types strictly carry the declared type:
     vpa.{min,max}Allowed.{cpu,memory}, resources.requests.{cpu,memory},
     resources.limits.memory and applicationVersion must be strings;
     keda.temporal.targetQueueSize must be an integer. A wrong scalar type
     parses fine for rules 2-9 but fails the chart's values.schema.json at
     Helm-apply time — blocking the release and freezing the TWD — so we catch
     it in CI. resources.limits.cpu (schema allows integer|string) and the
     serverResources/workerResources blocks (partly unconstrained) are
     deliberately excluded to avoid false positives.
 11. len(workerPools)       <= 10. Each pool renders its own TWD plus an
     on-demand mirror variant, so per-app tiers cost Temporal twice their
     count in worker deployments and don't scale across the fleet.
 12. Per pool: rules 2-8 again, against the pool's own `resources` and `vpa`
     blocks, resolved through the chart's PER-POOL fallback chains — which
     differ from the app-level ones (see _check_worker_pools).
 13. Per pool: rule 9 again, against the pool's own `keda` block.
 14. Every pool has a `name`; `name` and `taskQueue` are unique across pools.
 15. Declared workerPools require splitDeploymentEnabled=true AND
     temporalWorkerDeployment.enabled=true — the chart drops the whole block
     otherwise, so activities routed to a pool queue sit unpolled.

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

# Cap on dedicated worker pools per app. Every pool TWD also declares an
# on-demand (-od) mirror variant, so N pools cost Temporal 2N worker-deployment
# children on top of the main worker. Fleet-wide (100+ apps) an unbounded
# per-app tier count is what makes TWD reconciliation unmanageable. Change
# requires PR + platform-team review.
MAX_WORKER_POOLS: int = 10

# Chart-shipped per-pool vpa.maxAllowed fallback, used when neither the pool nor
# the app declares one. Deliberately separate from DEFAULT_VPA_MAX_* above: the
# per-pool chain is pool.vpa.maxAllowed -> vpa.maxAllowed -> this, and Helm's
# `default` swaps the WHOLE dict at each step rather than merging keys.
DEFAULT_POOL_VPA_MAX_CPU_MILLI: int = 2_000
DEFAULT_POOL_VPA_MAX_MEMORY_BYTES: int = 6 * 1024**3

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
    label: str = "vpa",
) -> tuple[dict[tuple[str, str], int | None], list[Violation]]:
    """Parse vpa.{minAllowed,maxAllowed} once. Shared by _check_vpa and
    _resolve_effective_vpa_max to avoid duplicate invalid_quantity violations.

    *label* prefixes the reported field path; per-pool callers pass
    `workerPools[i].vpa`."""
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
                    src[resource], f"{label}.{kind}.{resource}", errs
                )
    return parsed, errs


def _check_vpa(
    cfg: dict,
    parsed: dict[tuple[str, str], int | None],
    label: str = "vpa",
) -> list[Violation]:
    vpa = cfg.get("vpa") or {}
    mn = vpa.get("minAllowed") or {}
    mx = vpa.get("maxAllowed") or {}
    errs: list[Violation] = []

    cpu_max = parsed.get(("cpu", "maxAllowed"))
    if cpu_max is not None and cpu_max > MAX_VPA_CPU_MILLI:
        errs.append(
            Violation(
                field=f"{label}.maxAllowed.cpu",
                actual=mx.get("cpu"),
                expected=f"<= 7 cores ({MAX_VPA_CPU_MILLI}m)",
                rule="vpa_max_cpu_ceiling",
                fix=f"Lower {label}.maxAllowed.cpu to 7 cores or less.",
            )
        )

    mem_max = parsed.get(("memory", "maxAllowed"))
    if mem_max is not None and mem_max > MAX_VPA_MEMORY_BYTES:
        errs.append(
            Violation(
                field=f"{label}.maxAllowed.memory",
                actual=mx.get("memory"),
                expected="<= 27Gi",
                rule="vpa_max_memory_ceiling",
                fix=f"Lower {label}.maxAllowed.memory to 27Gi or less.",
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
                    field=f"{label}.minAllowed.{resource}",
                    actual=mn[resource],
                    expected=f"<= {label}.maxAllowed.{resource} ({mx[resource]})",
                    rule="vpa_min_le_max",
                    fix=(
                        f"Lower {label}.minAllowed.{resource} or raise "
                        f"{label}.maxAllowed.{resource}."
                    ),
                )
            )
    return errs


def _vpa_update_mode_is_off(update_mode: Any) -> bool:
    """True when this updateMode leaves initial requests unclamped.

    VPA clamps requests only in `Initial`, `Recreate`, or `Auto`. In `Off` it
    emits recommendations without applying them, so requests above maxAllowed
    deploy as-is.

    Matches the enum case-insensitively. K8s VPA accepts only the canonical
    casings, but app owners typing `off` should not get a misleading clamp
    violation — the chart's schema rejects bad casings later anyway. Also
    accepts bool False: PyYAML's YAML 1.1 loader coerces unquoted `off` / `no`
    to False (Helm's YAML 1.2 loader keeps the string), a common gotcha.
    """
    return update_mode is False or (
        isinstance(update_mode, str) and update_mode.strip().lower() == "off"
    )


def _resolve_effective_vpa_max(
    cfg: dict,
    parsed: dict[tuple[str, str], int | None],
    vpa_enabled: bool | None,
) -> tuple[int | None, int | None]:
    """Effective vpa.maxAllowed (cpu_milli, mem_bytes), or (None, None) when
    VPA does not clamp requests at admission.

    VPA clamps requests only in updateMode `Initial`, `Recreate`, or `Auto`.
    In `Off` mode VPA emits recommendations but doesn't apply them — initial
    requests above maxAllowed deploy as-is. Skip the requests<=vpa.maxAllowed
    rule in that case. Same when vpa.enabled is false/missing.

    Falls back to DEFAULT_VPA_MAX_* when maxAllowed not declared.
    """
    if vpa_enabled is not True:
        return None, None
    vpa = cfg.get("vpa") or {}
    if _vpa_update_mode_is_off(vpa.get("updateMode")):
        return None, None
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
    label: str | None = None,
    vpa_label: str = "vpa",
) -> list[Violation]:
    """Validate resources / serverResources / workerResources.
    None vpa_max_* skips the requests<=vpa.maxAllowed rule (vpa disabled).

    *label* overrides the reported field path when *cfg* is not the config root;
    per-pool callers pass the pool dict with `workerPools[i].resources`.
    *vpa_label* names the VPA block that clamps this one — a pool is clamped by
    its own, so the fix text points at the knob that actually moves."""
    label = label or key
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
                    src[resource], f"{label}.{kind}.{resource}", errs
                )

    # Even without VPA, raw request above infra guarantee fails to schedule.
    cpu_req = parsed.get(("cpu", "requests"))
    if cpu_req is not None and cpu_req > MAX_VPA_CPU_MILLI:
        errs.append(
            Violation(
                field=f"{label}.requests.cpu",
                actual=requests.get("cpu"),
                expected=f"<= 7 cores ({MAX_VPA_CPU_MILLI}m)",
                rule="requests_cpu_ceiling",
                fix=f"Lower {label}.requests.cpu to 7 cores or less.",
            )
        )
    mem_req = parsed.get(("memory", "requests"))
    if mem_req is not None and mem_req > MAX_VPA_MEMORY_BYTES:
        errs.append(
            Violation(
                field=f"{label}.requests.memory",
                actual=requests.get("memory"),
                expected="<= 27Gi",
                rule="requests_memory_ceiling",
                fix=f"Lower {label}.requests.memory to 27Gi or less.",
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
                field=f"{label}.requests.cpu",
                actual=requests.get("cpu"),
                expected=f"<= {vpa_label}.maxAllowed.cpu ({vpa_max_cpu_milli}m)",
                rule="requests_exceeds_vpa_max_cpu",
                fix=(
                    f"Lower {label}.requests.cpu, raise {vpa_label}.maxAllowed.cpu, "
                    f"or disable {vpa_label}.enabled."
                ),
            )
        )
    if (
        mem_req is not None
        and vpa_max_memory_bytes is not None
        and mem_req > vpa_max_memory_bytes
    ):
        errs.append(
            Violation(
                field=f"{label}.requests.memory",
                actual=requests.get("memory"),
                expected=f"<= {vpa_label}.maxAllowed.memory ({vpa_max_memory_bytes} bytes)",
                rule="requests_exceeds_vpa_max_memory",
                fix=(
                    f"Lower {label}.requests.memory, raise "
                    f"{vpa_label}.maxAllowed.memory, or disable {vpa_label}.enabled."
                ),
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
                    field=f"{label}.requests.{resource}",
                    actual=requests[resource],
                    expected=f"<= {label}.limits.{resource} ({limits[resource]})",
                    rule="requests_le_limits",
                    fix=f"Ensure {label}.requests.{resource} is not greater than {label}.limits.{resource}.",
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


def _check_keda(cfg: dict, label: str = "keda") -> list[Violation]:
    """keda.minReplicaCount <= keda.maxReplicaCount (when both set).

    Non-int / bool replica counts silently skip — chart schema handles type errors.
    *label* prefixes the reported field path; per-pool callers pass
    `workerPools[i].keda`.
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
                field=f"{label}.minReplicaCount",
                actual=mn,
                expected=f"<= {label}.maxReplicaCount ({mx})",
                rule="keda_min_le_max",
                fix=(
                    f"Lower {label}.minReplicaCount or raise "
                    f"{label}.maxReplicaCount."
                ),
            )
        ]
    return []


# Scalar-type contract mirrored from the atlan-app chart's values.schema.json.
# The chart's JSON schema rejects a wrong scalar type at Helm-apply time, which
# blocks the release and (for TWC apps) freezes the worker deployment because
# the TWD is never re-rendered. Catching it here fails the PR instead. Only
# fields the chart types UNAMBIGUOUSLY are listed: resources.limits.cpu (schema
# allows integer|string) and the server/workerResources blocks (partly
# unconstrained) are omitted so we never flag a value the chart would accept.
# Source of truth: atlan repo subcharts/atlan-app/values.schema.json.
_STRING_TYPED_FIELDS: tuple[str, ...] = (
    "applicationVersion",
    "vpa.minAllowed.cpu",
    "vpa.minAllowed.memory",
    "vpa.maxAllowed.cpu",
    "vpa.maxAllowed.memory",
    "resources.requests.cpu",
    "resources.requests.memory",
    "resources.limits.memory",
)
_INT_TYPED_FIELDS: tuple[str, ...] = ("keda.temporal.targetQueueSize",)


def _lookup(cfg: dict, path: str) -> tuple[Any, bool]:
    """Resolve a dotted path. Returns (value, present); present is False when
    any segment is missing or an intermediate node is not a mapping."""
    node: Any = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _check_scalar_types(cfg: dict) -> list[Violation]:
    """Type-check scalars the chart schema types strictly.

    A bare `cpu: 4` (int) or a quoted `targetQueueSize: "25"` (str) satisfies
    every magnitude rule but fails the chart's values.schema.json at deploy,
    blocking the release. Emit invalid_type so it fails in CI instead. Kept
    separate from the magnitude rules so those still run and report normally.
    """
    errs: list[Violation] = []
    for path in _STRING_TYPED_FIELDS:
        val, present = _lookup(cfg, path)
        if not present or isinstance(val, str):
            continue
        leaf = path.rsplit(".", 1)[-1]
        errs.append(
            Violation(
                field=path,
                actual=val,
                expected="string (quoted)",
                rule="invalid_type",
                fix=(
                    f'Quote {path} as a string, e.g. {leaf}: "{val}". The chart '
                    f"requires a string here; a bare {type(val).__name__} fails "
                    "Helm schema validation at deploy time and blocks the release."
                ),
            )
        )
    for path in _INT_TYPED_FIELDS:
        val, present = _lookup(cfg, path)
        if not present:
            continue
        # bool is an int subclass — reject it explicitly (the chart schema does).
        if isinstance(val, bool) or not isinstance(val, int):
            errs.append(
                Violation(
                    field=path,
                    actual=val,
                    expected="integer (unquoted)",
                    rule="invalid_type",
                    fix=(
                        f"Set {path} to an unquoted integer (got "
                        f"{type(val).__name__}). The chart requires an integer "
                        "here; the wrong type fails Helm schema validation at "
                        "deploy time and blocks the release."
                    ),
                )
            )
    return errs


def _resolve_pool_vpa_max(
    cfg: dict,
    pool: dict,
    pool_parsed: dict[tuple[str, str], int | None],
    app_parsed: dict[tuple[str, str], int | None],
) -> tuple[int | None, int | None]:
    """Effective per-pool vpa.maxAllowed (cpu_milli, mem_bytes). None per
    resource means the pool's VPA does not clamp that resource at admission.

    Three ways the chart's per-pool chain differs from the app-level one:
      - The pool VPA is gated on the POOL's own `vpa.enabled`; it does not
        inherit the app-level flag, so a pool without it is never clamped.
      - updateMode falls back pool.vpa.updateMode -> vpa.updateMode -> "Auto",
        so an undeclared updateMode still clamps.
      - maxAllowed swaps the whole dict at each fallback step, so a pool
        declaring only `memory` leaves cpu uncapped — hence a per-resource None
        rather than reaching for a chart default one key at a time.
    """
    pool_vpa = pool.get("vpa") or {}
    if pool_vpa.get("enabled") is not True:
        return None, None
    app_vpa = cfg.get("vpa") or {}
    update_mode = (
        pool_vpa["updateMode"]
        if "updateMode" in pool_vpa
        else app_vpa.get("updateMode")
    )
    if _vpa_update_mode_is_off(update_mode):
        return None, None

    declared = pool_vpa.get("maxAllowed") or {}
    parsed = pool_parsed
    if not declared:
        declared, parsed = app_vpa.get("maxAllowed") or {}, app_parsed
    if not declared:
        return DEFAULT_POOL_VPA_MAX_CPU_MILLI, DEFAULT_POOL_VPA_MAX_MEMORY_BYTES
    return (
        parsed.get(("cpu", "maxAllowed")) if "cpu" in declared else None,
        parsed.get(("memory", "maxAllowed")) if "memory" in declared else None,
    )


def _check_pool_identity(pools: list) -> list[Violation]:
    """Every pool needs a `name`; `name` and `taskQueue` must be unique.

    A duplicate name is a hard Helm failure, not a soft one: deployment.yaml
    keys pools as `pool:<name>` and looks the entry back up by name, so two
    entries sharing a name render two objects with the same metadata.name. A
    missing name renders a nameless TWD. Two pools on one taskQueue both poll
    it, so routed activities land on whichever worker grabs the task first and
    the tiers stop separating work.
    """
    errs: list[Violation] = []
    seen_names: dict[str, int] = {}
    seen_queues: dict[str, int] = {}
    for i, pool in enumerate(pools):
        if not isinstance(pool, dict):
            continue
        name = pool.get("name")
        if not isinstance(name, str) or not name.strip():
            errs.append(
                Violation(
                    field=f"workerPools[{i}].name",
                    actual=name,
                    expected="non-empty string",
                    rule="worker_pool_name_required",
                    fix=(
                        "Give every workerPools entry a name. The chart derives "
                        "the pool's TemporalWorkerDeployment name and its default "
                        "task queue from it."
                    ),
                )
            )
        elif name in seen_names:
            errs.append(
                Violation(
                    field=f"workerPools[{i}].name",
                    actual=name,
                    expected=(
                        "unique across workerPools (already used by "
                        f"workerPools[{seen_names[name]}])"
                    ),
                    rule="worker_pool_name_duplicate",
                    fix=(
                        "Rename this pool. The chart derives each pool's TWD name "
                        "from it, so two pools sharing a name render two objects "
                        "with the same metadata.name and the Helm apply fails."
                    ),
                )
            )
        else:
            seen_names[name] = i

        queue = pool.get("taskQueue")
        if not isinstance(queue, str) or not queue.strip():
            continue
        if queue in seen_queues:
            errs.append(
                Violation(
                    field=f"workerPools[{i}].taskQueue",
                    actual=queue,
                    expected=(
                        "unique across workerPools (already used by "
                        f"workerPools[{seen_queues[queue]}])"
                    ),
                    rule="worker_pool_task_queue_duplicate",
                    fix=(
                        "Give each pool its own taskQueue. Two pools polling one "
                        "queue both receive its activities, so routed work lands "
                        "on whichever worker grabs it first and the pools stop "
                        "separating load."
                    ),
                )
            )
        else:
            seen_queues[queue] = i
    return errs


def _check_worker_pools_rendered(bools: dict[str, bool | None]) -> list[Violation]:
    """Declared pools require splitDeploymentEnabled + temporalWorkerDeployment.

    Both chart defaults are false, so a missing flag drops the block exactly as
    an explicit false does — hence `is not True` rather than the
    explicit-false-only test _check_split_requires_twc uses (there, absent means
    the safe default; here it means the pools vanish). The CI driver runs SDK
    flag injection first, which sets both true for SDK >= 2.7.4, so this fires
    on apps that opt out explicitly or sit below the TWC floor — the cases where
    the app routes activities to a queue no worker polls and the workflow hangs.
    """
    unmet = [
        field
        for field in ("splitDeploymentEnabled", "temporalWorkerDeployment.enabled")
        if bools.get(field) is not True
    ]
    if not unmet:
        return []
    return [
        Violation(
            field="workerPools",
            actual=unmet,
            expected=(
                "splitDeploymentEnabled: true and "
                "temporalWorkerDeployment.enabled: true"
            ),
            rule="worker_pools_require_split_twc",
            fix=(
                "Set the listed flags to true, or remove the workerPools block. "
                "The chart renders pool deployments only under split + TWC and "
                "drops the block silently otherwise, so activities routed to a "
                "pool's task queue sit unpolled and the workflow hangs."
            ),
        )
    ]


def _check_worker_pools(
    cfg: dict,
    bools: dict[str, bool | None],
    app_vpa_parsed: dict[tuple[str, str], int | None],
) -> list[Violation]:
    """Validate `workerPools` — the extra dedicated worker pools (rules 11-15).

    Each entry renders its own TemporalWorkerDeployment + KEDA ScaledObject
    (+ optional VPA) polling its own task queue. The magnitude rules are the
    app-level ones re-pointed at the pool's blocks, but the chart resolves a
    pool's values through different fallback chains:
      - `pool.resources` REPLACES workerResources/resources wholesale (no key
        merge), so a pool declaring only requests has no limits at all — the
        requests<=limits rule simply has nothing to compare.
      - pool VPA: see _resolve_pool_vpa_max.
      - pool KEDA targetQueueSize is flat, not nested under `temporal`.
    """
    pools = cfg.get("workerPools")
    if pools is None:
        return []
    if not isinstance(pools, list):
        return [
            Violation(
                field="workerPools",
                actual=pools,
                expected="list of pool mappings",
                rule="invalid_type",
                fix=(
                    f"Make workerPools a YAML list (got {type(pools).__name__}). "
                    "The chart ranges over it; a non-list fails at render."
                ),
            )
        ]
    if not pools:
        return []

    errs = _check_worker_pools_rendered(bools)
    if len(pools) > MAX_WORKER_POOLS:
        errs.append(
            Violation(
                field="workerPools",
                actual=len(pools),
                expected=f"<= {MAX_WORKER_POOLS} pools",
                rule="worker_pool_count_ceiling",
                fix=(
                    f"Reduce workerPools to {MAX_WORKER_POOLS} or fewer. Each pool "
                    "renders its own TemporalWorkerDeployment plus an on-demand "
                    "mirror variant, so Temporal-side worker deployments grow at "
                    "twice the tier count per app."
                ),
            )
        )
    errs += _check_pool_identity(pools)

    for i, pool in enumerate(pools):
        prefix = f"workerPools[{i}]"
        if not isinstance(pool, dict):
            errs.append(
                Violation(
                    field=prefix,
                    actual=pool,
                    expected="mapping",
                    rule="invalid_type",
                    fix=(
                        f"Make each workerPools entry a mapping (got "
                        f"{type(pool).__name__})."
                    ),
                )
            )
            continue
        pool_parsed, parse_errs = _parse_vpa(pool, label=f"{prefix}.vpa")
        errs += parse_errs
        errs += _check_vpa(pool, pool_parsed, label=f"{prefix}.vpa")
        cpu_max, mem_max = _resolve_pool_vpa_max(cfg, pool, pool_parsed, app_vpa_parsed)
        errs += _check_resource_block(
            pool,
            "resources",
            cpu_max,
            mem_max,
            label=f"{prefix}.resources",
            vpa_label=f"{prefix}.vpa",
        )
        errs += _check_keda(pool, label=f"{prefix}.keda")
    return errs


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
    errs += _check_scalar_types(cfg)
    errs += _check_split_requires_twc(cfg, bools, parsed_sdk)
    errs += _check_twc_sdk_floor(bools, parsed_sdk)
    errs += _check_vpa(cfg, vpa_parsed)
    errs += _check_resources(cfg, vpa_parsed, bools)
    errs += _check_keda(cfg)
    errs += _check_worker_pools(cfg, bools, vpa_parsed)

    if errs:
        raise ConfigValidationError(errs)
