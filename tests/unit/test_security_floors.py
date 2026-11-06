"""Every CVE floor in ``[tool.uv] constraint-dependencies`` reaches consumers.

``constraint-dependencies`` pins this repo's own ``uv.lock`` only: it is not
published metadata, so an app that installs the wheel never sees it. A floor
that guards a runtime package therefore has to be published as a requirement
too (the way certifi is), or a connector app upgrading the SDK can still
resolve the vulnerable release.

Checked per package and per install scope (core, then core plus each extra):
every constrained package the scope installs must be published, by a package
of this tree that the scope installs, with a floor at least the constraint's.
A constraint on a package no scope installs (a dev/test-only tool such as
filelock) has no consumer to reach and is skipped.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]

# (pyproject, uv.lock) of each published package that declares constraints.
PACKAGES = [
    (ROOT / "pyproject.toml", ROOT / "uv.lock"),
    (
        ROOT / "packages/conformance/pyproject.toml",
        ROOT / "packages/conformance/uv.lock",
    ),
]

_LOWER_BOUND_OPS = {">=", ">", "==", "===", "~="}

# The platforms a consumer installs the wheel on. A published requirement is
# evaluated on each (with each Python the package declares) rather than on the
# test host only: a floor carried by a `sys_platform == 'linux'` entry does not
# protect a Windows install.
_PLATFORMS = [
    {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "platform_machine": machine,
    }
    for machine in ("x86_64", "aarch64")
] + [
    {
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "os_name": "posix",
        "platform_machine": "arm64",
    },
    {
        "sys_platform": "win32",
        "platform_system": "Windows",
        "os_name": "nt",
        "platform_machine": "AMD64",
    },
]

# A lower bound: (version, strict). Ordered so that a published floor covers a
# constraint exactly when it is >= it: `>1.0` covers `>=1.0`, not the reverse.
Floor = tuple[Version, bool]


def _floor(spec: SpecifierSet) -> Floor | None:
    """The tightest lower bound ``spec`` sets, or None if it sets none."""
    floors = [
        (Version(s.version.removesuffix(".*")), s.operator == ">")
        for s in spec
        if s.operator in _LOWER_BOUND_OPS
    ]
    return max(floors) if floors else None


def _targets(python_versions: list[str]) -> list[dict[str, str]]:
    """Marker environments for every declared Python on every platform."""
    return [
        {**plat, "python_version": v, "python_full_version": f"{v}.0"}
        for plat in _PLATFORMS
        for v in python_versions
    ]


def _applies(marker: str | None, extras: frozenset[str], env: dict[str, str]) -> bool:
    """Whether a requires-dist entry is installed in ``env`` with ``extras`` requested."""
    if not marker:
        return True
    m = Marker(marker)
    return any(m.evaluate({**env, "extra": e}) for e in (extras or {""}))


def _missing_floors(
    lock: dict,
    root: str,
    root_extras: frozenset[str],
    targets: list[dict[str, str]],
    constraints: list[str],
) -> list[str]:
    """Constraints the scope ``root[root_extras]`` installs but publishes no floor for.

    Walks the lock from ``root`` (dependencies plus requested extras, never dev
    groups). The published requirements are the ``requires-dist`` of the local
    packages the walk reaches: those are the wheels this tree ships. A floor
    must hold on every target environment, not just one. Lock edges are walked
    regardless of their markers, which can only report more, never less.
    """
    packages = {canonicalize_name(p["name"]): p for p in lock["package"]}
    seen: set[tuple[str, frozenset[str]]] = set()
    stack = [(canonicalize_name(root), root_extras)]
    published: dict[str, list[tuple[str | None, frozenset[str], Floor | None]]] = {}
    while stack:
        name, extras = stack.pop()
        if (name, extras) in seen or name not in packages:
            continue
        seen.add((name, extras))
        pkg = packages[name]
        edges = list(pkg.get("dependencies", []))
        for extra in extras:
            edges += pkg.get("optional-dependencies", {}).get(extra, [])
        stack += [
            (canonicalize_name(d["name"]), frozenset(d.get("extra", []))) for d in edges
        ]
        for req in pkg.get("metadata", {}).get("requires-dist", []):
            published.setdefault(canonicalize_name(req["name"]), []).append(
                (
                    req.get("marker"),
                    extras,
                    _floor(SpecifierSet(req.get("specifier", ""))),
                )
            )
    installed = {name for name, _ in seen}
    missing = []
    for raw in constraints:
        c = Requirement(raw)
        name = canonicalize_name(c.name)
        floor = _floor(c.specifier)
        if name not in installed or floor is None:
            continue
        uncovered = [
            env
            for env in targets
            if not any(
                p is not None and p >= floor and _applies(marker, extras, env)
                for marker, extras, p in published.get(name, [])
            )
        ]
        if uncovered:
            where = sorted(
                {f"{e['sys_platform']}/py{e['python_version']}" for e in uncovered}
            )
            missing.append(f"{raw} (not published for {', '.join(where)})")
    return missing


@pytest.mark.parametrize(
    ("pyproject", "lock_path"), PACKAGES, ids=lambda p: str(p.relative_to(ROOT))
)
def test_every_constraint_floor_on_a_runtime_package_is_published(
    pyproject: Path, lock_path: Path
) -> None:
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    constraints = config["tool"]["uv"].get("constraint-dependencies", [])
    project = config["project"]
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    scopes = [frozenset()] + [
        frozenset({e}) for e in project.get("optional-dependencies", {})
    ]
    pythons = [
        c.removeprefix("Programming Language :: Python :: ")
        for c in project["classifiers"]
        if c.startswith("Programming Language :: Python :: 3.")
    ]
    assert pythons, "no Python classifiers to evaluate markers against"
    targets = _targets(pythons)
    missing = {
        "core" if not s else f"[{next(iter(s))}]": m
        for s in scopes
        if (m := _missing_floors(lock, project["name"], s, targets, constraints))
    }
    assert not missing, (
        "constraint-dependencies floors that consumers never inherit; publish each "
        f"as a requirement (see certifi in {pyproject.name}): {missing}"
    )


# ---------------------------------------------------------------- the checker itself

TARGETS = _targets(["3.11", "3.12"])

_LOCK = {
    "package": [
        {
            "name": "app",
            "dependencies": [{"name": "lib"}],
            "optional-dependencies": {"x": [{"name": "extra-only"}]},
            "dev-dependencies": {"dev": [{"name": "devtool"}]},
            "metadata": {
                "requires-dist": [
                    {"name": "lib", "specifier": ">=1"},
                    {"name": "vuln", "specifier": ">=2.0,<3"},
                    {"name": "extra-only", "marker": "extra == 'x'"},
                ]
            },
        },
        {"name": "lib", "dependencies": [{"name": "vuln"}, {"name": "deep"}]},
        {"name": "vuln"},
        {"name": "deep"},
        {"name": "extra-only", "dependencies": [{"name": "deep"}]},
        {"name": "devtool"},
    ]
}


def test_a_published_floor_at_or_above_the_constraint_passes() -> None:
    assert _missing_floors(_LOCK, "app", frozenset(), TARGETS, ["vuln>=2.0"]) == []


def test_a_published_floor_below_the_constraint_fails() -> None:
    assert _missing_floors(_LOCK, "app", frozenset(), TARGETS, ["vuln>=2.1"])


def test_an_unpublished_transitive_runtime_package_fails() -> None:
    assert _missing_floors(_LOCK, "app", frozenset(), TARGETS, ["deep>=1"])


def test_dev_only_packages_are_skipped() -> None:
    assert _missing_floors(_LOCK, "app", frozenset(), TARGETS, ["devtool>=9"]) == []


def test_an_extra_installs_more_packages_than_core() -> None:
    assert _missing_floors(_LOCK, "app", frozenset(), TARGETS, ["extra-only>=1"]) == []
    assert _missing_floors(_LOCK, "app", frozenset({"x"}), TARGETS, ["extra-only>=1"])


def test_floor_reads_every_lower_bound_operator() -> None:
    assert _floor(SpecifierSet(">=1.2,<2")) == (Version("1.2"), False)
    assert _floor(SpecifierSet("~=1.9.1")) == (Version("1.9.1"), False)
    assert _floor(SpecifierSet("==2.0.*")) == (Version("2.0"), False)
    assert _floor(SpecifierSet(">1.0")) == (Version("1.0"), True)
    assert _floor(SpecifierSet("<2")) is None


def _single(spec: str, marker: str | None = None) -> dict:
    req = {"name": "m", "specifier": spec}
    if marker:
        req["marker"] = marker
    return {
        "package": [
            {
                "name": "app",
                "dependencies": [{"name": "m"}],
                "metadata": {"requires-dist": [req]},
            },
            {"name": "m"},
        ]
    }


def test_a_strict_constraint_is_not_covered_by_an_inclusive_floor() -> None:
    # `>=1.0` still admits 1.0, which `>1.0` excludes.
    assert _missing_floors(_single(">=1.0"), "app", frozenset(), TARGETS, ["m>1.0"])
    assert (
        _missing_floors(_single(">1.0"), "app", frozenset(), TARGETS, ["m>1.0"]) == []
    )
    assert (
        _missing_floors(_single(">1.0"), "app", frozenset(), TARGETS, ["m>=1.0"]) == []
    )


def test_a_floor_published_for_one_platform_does_not_cover_the_others() -> None:
    # Whatever host runs the test, a linux-only floor leaves Windows and macOS
    # consumers on the vulnerable release.
    lock = _single(">=2", "sys_platform == 'linux'")
    missing = _missing_floors(lock, "app", frozenset(), TARGETS, ["m>=2"])
    assert missing and "win32" in missing[0] and "linux" not in missing[0]


def test_platform_specific_floors_that_together_cover_every_target_pass() -> None:
    lock = _single(">=2", "sys_platform == 'win32'")
    lock["package"][0]["metadata"]["requires-dist"].append(
        {"name": "m", "specifier": ">=2", "marker": "sys_platform != 'win32'"}
    )
    assert _missing_floors(lock, "app", frozenset(), TARGETS, ["m>=2"]) == []


def test_a_floor_published_for_some_pythons_only_fails() -> None:
    lock = _single(">=2", "python_version >= '3.12'")
    missing = _missing_floors(lock, "app", frozenset(), TARGETS, ["m>=2"])
    assert missing and "py3.11" in missing[0] and "py3.12" not in missing[0]


def test_constraint_floors_are_compared_as_versions_not_strings() -> None:
    # "0.0.26" < "0.0.3" as strings; as versions it is the other way round.
    lock = {
        "package": [
            {
                "name": "app",
                "dependencies": [{"name": "m"}],
                "metadata": {"requires-dist": [{"name": "m", "specifier": ">=0.0.26"}]},
            },
            {"name": "m"},
        ]
    }
    assert _missing_floors(lock, "app", frozenset(), TARGETS, ["m>=0.0.3"]) == []
    assert _missing_floors(lock, "app", frozenset(), TARGETS, ["m>=0.0.27"])
