#!/usr/bin/env python3
"""Check the built api and SDK wheels the way the host and an upgrading app install them.

``check_api_surface.py`` reads the source; this installs the *built* wheels into
clean venvs, because an editable or path install exposes the whole
``application_sdk/`` tree and would pass every check without testing anything.

1. **Disjoint wheels.** Every file under ``application_sdk/`` ships in exactly
   one wheel: the listed files in the api wheel only, the rest in the SDK wheel,
   and every tracked source file in one of the two. (Two wheels owning one file
   means uninstalling either deletes it while the other still reports installed.)
2. **Upgrade from a release.** Starting from ``--from-release`` of
   ``atlan-application-sdk`` (which owned these files alone):

   * ``uv pip install -U`` the new wheels — how the fleet and the host install —
     and assert every listed file is on disk and the worker imports;
   * pip's uninstall of the old release deletes the files the api wheel has just
     written, so assert the documented recovery works:
     ``pip install --force-reinstall atlan-application-sdk-api``.
3. **Api alone.** Install only the api wheel (core dependencies), import every
   core module, and fail if any unlisted ``application_sdk`` module loads or the
   process exceeds ``--max-rss-mb``. Again with every extra installed, importing
   every listed module. Then serve a ``DefaultHandler`` with ``build_asgi_app``:
   ``/health`` is 200, a non-object body on ``/workflows/v1/auth`` is rejected,
   and auth / check / metadata answer.
4. **Each module alone.** Step 3 imports the modules in one process, in sorted
   order, so a module that only imports because an earlier one already loaded
   its dependency would pass. In both api venvs, ``python -I`` imports each
   listed module in a fresh process and names every one that fails.

The wheels are built the way ``tag-and-publish.yaml`` publishes them: plain
``uv build`` (an sdist, then the wheel built from that sdist). The probe picks
the ``.whl`` for each distribution and requires its sdist beside it, so a build
that skipped the sdist (``--wheel``, which builds from the tree) is reported.

Usage::

    uv build -o dist . && uv build -o dist packages/api
    python3 .github/scripts/probe_api_wheels.py --dist dist --from-release 3.39.1

``--from-release latest-tag`` starts from the newest ``vX.Y.Z`` tag reachable
from HEAD that is older than the built wheels (CI's choice: the release this
commit would upgrade from).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_api_surface  # noqa: E402
import gen_api_files  # noqa: E402
import import_smoke  # noqa: E402

_ALONE = r"""
import importlib, resource, sys
to_import = sys.argv[1].split(",")
listed = sys.argv[3].split(",")
for m in to_import:
    importlib.import_module(m)
extra = sorted(
    m for m in sys.modules
    if (m == "application_sdk" or m.startswith("application_sdk."))
    and m not in listed
    and getattr(sys.modules[m], "__file__", None)  # a namespace dir has no file
)
if extra:
    sys.exit(f"api-only install loaded unlisted modules: {extra[:10]}")
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
rss_mb = rss / 1024 / (1024 if sys.platform == "darwin" else 1)
if rss_mb > float(sys.argv[2]):
    sys.exit(f"api-only import RSS {rss_mb:.0f} MB exceeds {sys.argv[2]} MB")
from fastapi.testclient import TestClient
from application_sdk.handler import DefaultHandler
from application_sdk.handler.asgi import build_asgi_app
client = TestClient(build_asgi_app(DefaultHandler(), app_name="probe"), raise_server_exceptions=False)
assert client.get("/health").status_code == 200, "GET /health"
bad = client.post("/workflows/v1/auth", content=b"[1]", headers={"content-type": "application/json"})
# Rejected exactly as the worker rejects it (the split changes no behaviour).
assert bad.status_code >= 400, f"non-object body answered {bad.status_code}"
creds = {"credentials": [{"key": "username", "value": "u"}], "connection_config": {"host": "h"}}
for route in ("auth", "check", "metadata"):
    r = client.post(f"/workflows/v1/{route}", json=creds)
    assert r.status_code == 200 and isinstance(r.json(), dict), f"{route}: {r.status_code} {r.text[:200]}"
empty = client.post("/workflows/v1/auth", json={"credentials": []})
assert isinstance(empty.json(), dict), f"empty credentials answered a bare {empty.status_code}"

from application_sdk.errors import AuthError
class _Failing(DefaultHandler):
    async def test_auth(self, input):
        raise AuthError(message="bad password for postgres://u:secret@h/db")
failing = TestClient(build_asgi_app(_Failing(), app_name="probe"), raise_server_exceptions=False)
r = failing.post("/workflows/v1/auth", json=creds)
assert r.status_code != 500 or isinstance(r.json(), dict), "AppError answered a bare 500"
extra = sorted(
    m for m in sys.modules
    if (m == "application_sdk" or m.startswith("application_sdk."))
    and m not in listed
    and getattr(sys.modules[m], "__file__", None)  # a namespace dir has no file
)
if extra:
    sys.exit(f"serving requests loaded unlisted modules: {extra[:10]}")
print(f"{sys.argv[4]}: {len(to_import)} modules, {rss_mb:.0f} MB, auth/check/metadata + AppError path OK")
"""

_PRESENT = r"""
import sys, sysconfig
from pathlib import Path
site = Path(sysconfig.get_paths()["purelib"])
missing = [f for f in sys.argv[1].split(",") if not (site / f).is_file()]
if missing:
    sys.exit(f"upgrade left listed files missing: {missing}")
import application_sdk.execution, application_sdk.handler.service  # the worker still imports
print("upgrade: every listed file present, worker imports")
"""


def _version_tuple(text: str) -> tuple[int, ...] | None:
    parts = text.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def resolve_release(value: str, root: Path, below: str | None = None) -> str:
    """``value`` itself, or for ``latest-tag`` the newest reachable release tag.

    With ``below`` (the version being probed), the newest release strictly older
    than it. On main right after a release the built wheels carry the released
    version, and pip treats an equal version as already installed — the probe
    would upgrade nothing and test a mix no real upgrade produces.
    """
    if value != "latest-tag":
        return value
    tags = subprocess.run(
        ["git", "tag", "--list", "v[0-9]*", "--merged", "HEAD", "--sort=-v:refname"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    ceiling = _version_tuple(below) if below else None
    for tag in tags:
        version = tag.removeprefix("v")
        parsed = _version_tuple(version)
        if parsed is not None and (ceiling is None or parsed < ceiling):
            return version
    raise ValueError(f"no release tag below {below} reachable from HEAD")


def _wheel(dist: Path, prefix: str) -> Path:
    """The one wheel of distribution ``prefix`` in ``dist`` (sdists ignored)."""
    (wheel,) = [
        w for w in dist.glob(f"{prefix}-*.whl") if w.name.split("-")[0] == prefix
    ]
    return wheel


def sdist_problems(dist: Path, wheels: list[Path]) -> list[str]:
    """Each wheel must have the sdist it was built from beside it.

    ``tag-and-publish.yaml`` publishes from plain ``uv build``: an sdist, then a
    wheel built *from that sdist*. ``uv build --wheel`` builds from the source
    tree instead, so a file the sdist drops would ship in CI's wheel and be
    missing from the published one. A matching ``<name>-<version>.tar.gz`` is
    the evidence the wheel came the publish way.
    """
    problems = []
    for wheel in wheels:
        name, version = wheel.name.split("-")[:2]
        if not (dist / f"{name}-{version}.tar.gz").is_file():
            problems.append(
                f"{wheel.name}: no {name}-{version}.tar.gz beside it — build with "
                "plain `uv build` (sdist, then wheel from it), as the release does"
            )
    return problems


def import_each_alone(python: Path, modules: list[str], jobs: int = 8) -> list[str]:
    """Import every module in its own fresh ``python -I`` process.

    ``import_smoke.check_imports`` does the work (the conformance release gate
    uses it the same way); ``-I`` keeps the cwd and ``PYTHON*`` variables off
    ``sys.path``, so only the installed wheels are importable. Returns one line
    per module that did not import, naming it with the last line of its error.
    """
    return [
        f"{f.target}: {(f.output.splitlines() or ['(no output)'])[-1]}"
        for f in import_smoke.check_imports(str(python), modules, jobs)
    ]


def _digests(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as zf:
        return {
            n: hashlib.sha256(zf.read(n)).hexdigest()
            for n in zf.namelist()
            if n.startswith("application_sdk/")
        }


def partition(api: Path, sdk: Path, listed: list[str], tracked: set[str]) -> list[str]:
    """Each ``application_sdk/`` file in exactly one wheel, and the right one."""
    a, s = set(_digests(api)), set(_digests(sdk))
    problems = [f"{n}: ships in both wheels" for n in sorted(a & s)]
    problems += [
        f"{n}: listed but not in the api wheel" for n in sorted(set(listed) - a)
    ]
    problems += [
        f"{n}: in the api wheel but not listed" for n in sorted(a - set(listed))
    ]
    problems += [f"{n}: in neither wheel" for n in sorted(tracked - a - s)]
    return problems


def tracked_files(root: Path) -> set[str]:
    """Source files under ``application_sdk/`` that git tracks."""
    out = subprocess.run(
        ["git", "ls-files", "application_sdk"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line for line in out.splitlines() if line}


def _venv(where: Path) -> Path:
    subprocess.run(["uv", "venv", "-q", str(where)], check=True)
    return where / "bin" / "python"


def _pip(python: Path, *args: str) -> None:
    subprocess.run(
        ["uv", "pip", "install", "-q", "--python", str(python), *args], check=True
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--from-release", required=True)
    parser.add_argument("--max-rss-mb", type=float, default=120)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    api = _wheel(args.dist, "atlan_application_sdk_api")
    sdk = _wheel(args.dist, "atlan_application_sdk")
    listed = check_api_surface.listed_files(args.root)
    failures = sdist_problems(args.dist, [api, sdk])
    failures += partition(api, sdk, listed, tracked_files(args.root))

    with tempfile.TemporaryDirectory() as tmp:
        built = sdk.name.split("-")[1]
        release = resolve_release(args.from_release, args.root, below=built)

        # uv: how the fleet image and the host install. Must just work.
        py = _venv(Path(tmp) / "upgrade-uv")
        _pip(py, f"atlan-application-sdk=={release}")
        _pip(py, "-U", str(api), str(sdk))
        done = subprocess.run([str(py), "-c", _PRESENT, ",".join(listed)], cwd=tmp)
        if done.returncode:
            failures.append(
                f"uv upgrade from {release} left a broken install (listed files "
                "missing, or the worker does not import)"
            )

        # pip: its uninstall of the old release deletes the files the api wheel
        # just wrote. The release note gives the recovery; assert it works.
        py = _venv(Path(tmp) / "upgrade-pip")
        _pip(py, f"atlan-application-sdk=={release}")
        _pip(py, "pip")
        subprocess.run(
            [str(py), "-m", "pip", "install", "-q", "-U", str(api), str(sdk)],
            check=True,
        )
        subprocess.run(
            [
                str(py),
                "-m",
                "pip",
                "install",
                "-q",
                "--force-reinstall",
                "--no-deps",
                str(api),
            ],
            check=True,
        )
        done = subprocess.run([str(py), "-c", _PRESENT, ",".join(listed)], cwd=tmp)
        if done.returncode:
            failures.append(
                f"pip upgrade from {release} plus `pip install --force-reinstall "
                "atlan-application-sdk-api` did not restore a working install"
            )

        every = ",".join(check_api_surface.module_of(f) for f in listed)
        groups = gen_api_files.file_groups(args.root)
        core = ",".join(
            check_api_surface.module_of(f)
            for f in listed
            if "core" in groups.get(f, {"core"})
        )
        extras = ",".join(
            sorted(g for g in gen_api_files.seed_groups(args.root) if g != "core")
        )
        # cwd outside the repo: ``python -c`` puts the cwd on sys.path, and the
        # source tree there would hide what the wheel is missing.
        for leg, requirement, modules, budget in (
            # The core install is what every hosted app pays: held to the budget.
            ("api core", str(api), core, args.max_rss_mb),
            # Every extra installed: every listed module must import and serve.
            (
                f"api [{extras}]",
                f"atlan-application-sdk-api[{extras}] @ {api.resolve().as_uri()}",
                every,
                10_000,
            ),
        ):
            py = _venv(Path(tmp) / leg.replace(" ", "-").strip("[]"))
            _pip(py, requirement, "httpx")
            done = subprocess.run(
                [str(py), "-c", _ALONE, modules, str(budget), every, leg], cwd=tmp
            )
            if done.returncode:
                failures.append(f"{leg} install failed")
            names = modules.split(",")
            alone = import_each_alone(py, names, jobs=min(16, os.cpu_count() or 4))
            failures += [f"{leg}: does not import alone: {a}" for a in alone]
            print(f"{leg}: {len(names) - len(alone)}/{len(names)} modules import alone")

    for failure in failures:
        print(f"::error::{failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
