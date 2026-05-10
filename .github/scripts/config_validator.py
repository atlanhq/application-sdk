"""
Validate atlan.yaml deploy config against platform guardrails in CI.

Runs in the Build & Publish reusable workflow before image build, against the
DEPLOY_CONFIG block extracted by parse_atlan_yaml.py. Mirrors the pattern in
parse_atlan_yaml.py: parse YAML once, run a list of rule functions, raise on
violation. PyYAML is the only runtime dependency.

Rules enforced:
  1. splitDeploymentEnabled=true requires temporalWorkerDeployment.enabled=true
     (image-pull / crashloop only surfaces via TWC during version rollout).
  2. vpa.maxAllowed.cpu     <= 7 cores (7000m).
  3. vpa.maxAllowed.memory  <= 27Gi (binary, = 27 * 1024^3 bytes).
  4. requests.cpu           <= 7 cores  (resources / serverResources / workerResources).
  5. requests.memory        <= 27Gi.
  6. When vpa.enabled=true: requests.{cpu,memory} <= effective vpa.maxAllowed.
     Defaults applied when vpa.maxAllowed not declared: cpu=2000m, memory=18Gi
     (mirror chart-shipped defaults so validator's effective ceiling matches
     what VPA enforces in cluster).
  7. requests.{cpu,memory}  <= limits.{cpu,memory}  (sanity).
  8. vpa.minAllowed         <= vpa.maxAllowed per resource.
  9. keda.minReplicaCount   <= keda.maxReplicaCount.

Apply scope: rules 4-7 hit `resources` always; `serverResources` and
`workerResources` only when `splitDeploymentEnabled=true` (chart ignores them
otherwise).

On failure: raises ConfigValidationError carrying a list of Violation records.
The CI driver (validate_atlan_yaml.py) maps these to ::error file=atlan.yaml::
GitHub Actions annotations so violations show inline on the PR diff.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import yaml

# Hardcoded infra ceilings. Change requires a PR + platform team review.
MAX_VPA_CPU_MILLI: int = 7_000  # 7 cores
MAX_VPA_MEMORY_BYTES: int = 27 * 1024**3  # 27 Gi (binary)

# Defaults for vpa.maxAllowed when vpa.enabled=true but maxAllowed not declared
# in atlan.yaml. Mirror the chart-shipped defaults so validator's effective
# ceiling matches what VPA actually enforces in cluster.
DEFAULT_VPA_MAX_CPU_MILLI: int = 2_000  # 2 cores
DEFAULT_VPA_MAX_MEMORY_BYTES: int = 18 * 1024**3  # 18 Gi (binary)

# Memory suffix multipliers. Binary (Ki/Mi/Gi/...) are powers of 1024;
# decimal (k/K/M/G/...) are powers of 1000. They are NOT interchangeable.
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
    """One validation failure. Serialised to GH Actions annotations by the driver."""

    field: str
    actual: Any
    expected: Any
    rule: str
    fix: str

    def to_dict(self) -> dict:
        return asdict(self)


class ConfigValidationError(ValueError):
    """Raised when atlan.yaml fails one or more guardrail rules."""

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        body = "\n".join(
            f"- [{v.rule}] {v.field}={v.actual!r} (expected: {v.expected}). {v.fix}"
            for v in violations
        )
        super().__init__("atlan.yaml validation failed:\n" + body)


def parse_cpu(value: Any) -> int:
    """Parse a Kubernetes CPU quantity into millicores.

    Accepts int/float (treated as cores), "100m", "1", "1.5", "500m".
    Rejects malformed input with ValueError.
    """
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
    """Parse a Kubernetes memory quantity into bytes.

    Accepts int/float (treated as bytes), "500Mi", "1Gi", "27Gi", "1024".
    Binary suffixes (Ki/Mi/Gi/...) use 1024; decimal (k/K/M/G/...) use 1000.
    """
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


def _check_split_requires_twc(cfg: dict) -> list[Violation]:
    """Block only when the user has explicitly disabled TWC under split.

    TWC requires SDK >= 2.7.4. SDK flag injection only adds the
    `temporalWorkerDeployment` block on SDK >= 2.7.4. Apps on
    2.3.1 <= SDK < 2.7.4 receive `splitDeploymentEnabled: true` via injection
    but no TWC block — that is intentional and must not be blocked here.

    Fail only on the explicit opt-out: `temporalWorkerDeployment.enabled: false`
    while `splitDeploymentEnabled: true`. Missing field, missing `enabled` key,
    or `enabled: true` all pass.
    """
    if cfg.get("splitDeploymentEnabled") is not True:
        return []
    twd = cfg.get("temporalWorkerDeployment") or {}
    if twd.get("enabled") is not False:
        return []
    return [
        Violation(
            field="temporalWorkerDeployment.enabled",
            actual=False,
            expected="true (or omit the field — TWC requires SDK >= 2.7.4)",
            rule="twc_required_for_split",
            fix="Set temporalWorkerDeployment.enabled: true, or remove the temporalWorkerDeployment block. Split-worker deployments must use TWC (when supported) so image-pull and crashloop failures surface during version rollout.",
        )
    ]


def _check_vpa(cfg: dict) -> list[Violation]:
    """Run all VPA rules off a single parse pass.

    Combines maxAllowed ceiling and minAllowed<=maxAllowed checks so each
    field is parsed once. A separate rule per function would re-invoke
    _safe_parse_* on the same field and emit duplicate `invalid_quantity`
    violations on malformed input.
    """
    vpa = cfg.get("vpa") or {}
    mn = vpa.get("minAllowed") or {}
    mx = vpa.get("maxAllowed") or {}
    errs: list[Violation] = []

    parsed: dict[tuple[str, str], int | None] = {}
    for kind, src in (("minAllowed", mn), ("maxAllowed", mx)):
        for resource, parser in (
            ("cpu", _safe_parse_cpu),
            ("memory", _safe_parse_memory),
        ):
            if resource in src:
                parsed[(resource, kind)] = parser(
                    src[resource], f"vpa.{kind}.{resource}", errs
                )

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


def _resolve_effective_vpa_max(cfg: dict) -> tuple[int | None, int | None]:
    """Compute effective vpa.maxAllowed for cpu/memory when vpa.enabled.

    Returns (cpu_milli, memory_bytes) or (None, None) when vpa is disabled.
    Falls back to chart defaults when vpa.maxAllowed is not declared in
    atlan.yaml. Parse failures yield None for that resource — invalid_quantity
    violations are already emitted by _check_vpa.
    """
    vpa = cfg.get("vpa") or {}
    if vpa.get("enabled") is not True:
        return None, None
    max_allowed = vpa.get("maxAllowed") or {}
    discard: list[Violation] = []  # parse errs already emitted by _check_vpa
    cpu_milli: int | None = DEFAULT_VPA_MAX_CPU_MILLI
    if "cpu" in max_allowed:
        cpu_milli = _safe_parse_cpu(max_allowed["cpu"], "vpa.maxAllowed.cpu", discard)
    mem_bytes: int | None = DEFAULT_VPA_MAX_MEMORY_BYTES
    if "memory" in max_allowed:
        mem_bytes = _safe_parse_memory(
            max_allowed["memory"], "vpa.maxAllowed.memory", discard
        )
    return cpu_milli, mem_bytes


def _check_resource_block(
    cfg: dict,
    key: str,
    vpa_max_cpu_milli: int | None = None,
    vpa_max_memory_bytes: int | None = None,
) -> list[Violation]:
    """Validate one of: resources / serverResources / workerResources.

    *vpa_max_cpu_milli* / *vpa_max_memory_bytes* — effective vpa.maxAllowed
    when vpa.enabled. None means VPA is disabled and the requests<=vpa.maxAllowed
    rule is skipped.
    """
    block = cfg.get(key) or {}
    if not block:
        return []
    requests = block.get("requests") or {}
    limits = block.get("limits") or {}
    errs: list[Violation] = []

    # Parse each (resource, kind) at most once. Without the cache, an invalid
    # quantity (e.g. memory: "abc") would emit duplicate `invalid_quantity`
    # violations across rules that touch the same field.
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

    # Rule: requests <= infra ceilings (mirror vpa.maxAllowed caps).
    # Even without VPA, a raw request that exceeds infra guarantee fails to
    # schedule. Same numeric ceilings as vpa.maxAllowed.
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

    # Rule: when vpa.enabled, requests <= vpa.maxAllowed (chart defaults
    # used when maxAllowed is not declared). Initial request that exceeds
    # vpa.maxAllowed will be clamped down by VPA at admission, surprising the
    # app owner — fail at config time instead.
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

    # Rule: requests <= limits (per resource).
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


def _check_resources(cfg: dict) -> list[Violation]:
    vpa_cpu, vpa_mem = _resolve_effective_vpa_max(cfg)
    errs = _check_resource_block(cfg, "resources", vpa_cpu, vpa_mem)
    if cfg.get("splitDeploymentEnabled") is True:
        for k in ("serverResources", "workerResources"):
            errs += _check_resource_block(cfg, k, vpa_cpu, vpa_mem)
    return errs


def _check_keda(cfg: dict) -> list[Violation]:
    """Rule: keda.minReplicaCount <= keda.maxReplicaCount (when both set).

    Non-int / bool replica counts silently skip this rule — type errors are
    deliberately surfaced by Helm chart rendering rather than reported as
    config_validator violations. Validator scope is semantic guardrails;
    basic type validation lives in the chart's schema.
    """
    keda = cfg.get("keda") or {}
    mn = keda.get("minReplicaCount")
    mx = keda.get("maxReplicaCount")
    if mn is None or mx is None:
        return []
    # bool is a subclass of int — reject explicitly to mirror the parse_cpu /
    # parse_memory guards.
    if isinstance(mn, bool) or isinstance(mx, bool):
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


def validate_config(config_yaml: Any) -> None:
    """Run all guardrail rules against *config_yaml*.

    Accepts either a YAML string or an already-parsed dict. The string path is
    the normal one (called from CI with the DEPLOY_CONFIG block extracted by
    parse_atlan_yaml.py). The dict path is a defensive escape hatch and lets
    tests pass dicts directly.

    No-op when the input parses to anything other than a mapping. Raises
    ConfigValidationError aggregating every violation found in a single pass —
    the user sees all problems at once rather than fixing them one-at-a-time
    across N submission attempts.
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

    errs: list[Violation] = []
    errs += _check_split_requires_twc(cfg)
    errs += _check_vpa(cfg)
    errs += _check_resources(cfg)
    errs += _check_keda(cfg)

    if errs:
        raise ConfigValidationError(errs)
