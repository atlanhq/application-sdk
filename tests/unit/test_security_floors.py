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


def _floor(spec: SpecifierSet) -> Version | None:
    """The lowest version ``spec`` admits a lower bound for, or None if unbounded."""
    floors = [
        Version(s.version.removesuffix(".*"))
        for s in spec
        if s.operator in _LOWER_BOUND_OPS
    ]
    return max(floors) if floors else None


def _applies(marker: str | None, extras: frozenset[str]) -> bool:
    """Whether a requires-dist entry is installed with ``extras`` requested."""
    if not marker:
        return True
    m = Marker(marker)
    return any(m.evaluate({"extra": e}) for e in (extras or {""}))


def _missing_floors(
    lock: dict, root: str, root_extras: frozenset[str], constraints: list[str]
) -> list[str]:
    """Constraints the scope ``root[root_extras]`` installs but publishes no floor for.

    Walks the lock from ``root`` (dependencies plus requested extras, never dev
    groups). The published requirements are the ``requires-dist`` of the local
    packages the walk reaches: those are the wheels this tree ships.
    """
    packages = {canonicalize_name(p["name"]): p for p in lock["package"]}
    seen: set[tuple[str, frozenset[str]]] = set()
    stack = [(canonicalize_name(root), root_extras)]
    published: dict[str, list[Version | None]] = {}
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
            if _applies(req.get("marker"), extras):
                published.setdefault(canonicalize_name(req["name"]), []).append(
                    _floor(SpecifierSet(req.get("specifier", "")))
                )
    installed = {name for name, _ in seen}
    missing = []
    for raw in constraints:
        c = Requirement(raw)
        name = canonicalize_name(c.name)
        floor = _floor(c.specifier)
        if name not in installed or floor is None:
            continue
        if not any(p is not None and p >= floor for p in published.get(name, [])):
            missing.append(f"{raw} (published: {published.get(name, 'nothing')})")
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
    missing = {
        "core" if not s else f"[{next(iter(s))}]": m
        for s in scopes
        if (m := _missing_floors(lock, project["name"], s, constraints))
    }
    assert not missing, (
        "constraint-dependencies floors that consumers never inherit; publish each "
        f"as a requirement (see certifi in {pyproject.name}): {missing}"
    )


# ---------------------------------------------------------------- the checker itself

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
    assert _missing_floors(_LOCK, "app", frozenset(), ["vuln>=2.0"]) == []


def test_a_published_floor_below_the_constraint_fails() -> None:
    assert _missing_floors(_LOCK, "app", frozenset(), ["vuln>=2.1"])


def test_an_unpublished_transitive_runtime_package_fails() -> None:
    assert _missing_floors(_LOCK, "app", frozenset(), ["deep>=1"])


def test_dev_only_packages_are_skipped() -> None:
    assert _missing_floors(_LOCK, "app", frozenset(), ["devtool>=9"]) == []


def test_an_extra_installs_more_packages_than_core() -> None:
    assert _missing_floors(_LOCK, "app", frozenset(), ["extra-only>=1"]) == []
    assert _missing_floors(_LOCK, "app", frozenset({"x"}), ["extra-only>=1"])


def test_floor_reads_every_lower_bound_operator() -> None:
    assert _floor(SpecifierSet(">=1.2,<2")) == Version("1.2")
    assert _floor(SpecifierSet("~=1.9.1")) == Version("1.9.1")
    assert _floor(SpecifierSet("==2.0.*")) == Version("2.0")
    assert _floor(SpecifierSet("<2")) is None


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
    assert _missing_floors(lock, "app", frozenset(), ["m>=0.0.3"]) == []
    assert _missing_floors(lock, "app", frozenset(), ["m>=0.0.27"])
