#!/usr/bin/env python3
"""Check the built api and SDK wheels the way the host and an upgrading app install them.

``check_api_surface.py`` reads the source; this installs the *built* wheels into
clean venvs, because an editable or path install exposes the whole
``application_sdk/`` tree and would pass every check without testing anything.

1. **Same bytes.** Every file in the api wheel is in the SDK wheel with identical
   content. The files ship in both so an upgrade never leaves them missing (2),
   and the SDK pins the api distribution to its own version, so both copies are
   always the same file.
2. **Upgrade from a release.** Starting from ``--from-release`` of
   ``atlan-application-sdk`` (which owned these files alone), ``pip install -U``
   the new wheels and assert every listed file is still on disk. With disjoint
   wheels pip's uninstall of the old release deletes the files the api wheel has
   just written, and nothing reports it.
3. **Api alone.** Install only the api wheel, import every listed module, and
   fail if any unlisted ``application_sdk`` module loads or the process exceeds
   ``--max-rss-mb``. Then serve a ``DefaultHandler`` with ``build_asgi_app``:
   ``/health`` is 200 and a non-object body on ``/workflows/v1/auth`` is 422.

Usage::

    uv build --wheel -o dist . && uv build --wheel -o dist packages/api
    python3 .github/scripts/probe_api_wheels.py --dist dist --from-release 3.39.1

``--from-release latest-tag`` starts from the newest ``vX.Y.Z`` tag reachable
from HEAD that is older than the built wheels (CI's choice: the release this
commit would upgrade from).
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_api_surface  # noqa: E402

_ALONE = r"""
import importlib, resource, sys
listed = sys.argv[1].split(",")
for m in listed:
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
client = TestClient(build_asgi_app(DefaultHandler(), app_name="probe"))
assert client.get("/health").status_code == 200, "GET /health"
bad = client.post("/workflows/v1/auth", content=b"[1]", headers={"content-type": "application/json"})
assert bad.status_code == 422, f"non-object body answered {bad.status_code}"
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
assert "secret" not in r.text, "AppError response leaked the DSN password"
extra = sorted(
    m for m in sys.modules
    if (m == "application_sdk" or m.startswith("application_sdk."))
    and m not in listed
    and getattr(sys.modules[m], "__file__", None)  # a namespace dir has no file
)
if extra:
    sys.exit(f"serving requests loaded unlisted modules: {extra[:10]}")
print(f"api alone: {len(listed)} modules, {rss_mb:.0f} MB, auth/check/metadata + AppError path OK")
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
    (wheel,) = [
        w for w in dist.glob(f"{prefix}-*.whl") if w.name.split("-")[0] == prefix
    ]
    return wheel


def _digests(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as zf:
        return {
            n: hashlib.sha256(zf.read(n)).hexdigest()
            for n in zf.namelist()
            if n.startswith("application_sdk/")
        }


def same_bytes(api: Path, sdk: Path) -> list[str]:
    a, s = _digests(api), _digests(sdk)
    return [
        f"{n}: {'missing from' if n not in s else 'differs in'} the SDK wheel"
        for n in sorted(a)
        if s.get(n) != a[n]
    ]


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
    failures = same_bytes(api, sdk)

    with tempfile.TemporaryDirectory() as tmp:
        py = _venv(Path(tmp) / "upgrade")
        _pip(
            py,
            f"atlan-application-sdk=={resolve_release(args.from_release, args.root)}",
        )
        _pip(py, "pip")
        subprocess.run(
            [str(py), "-m", "pip", "install", "-q", "-U", str(api), str(sdk)],
            check=True,
        )
        done = subprocess.run([str(py), "-c", _PRESENT, ",".join(listed)], cwd=tmp)
        if done.returncode:
            failures.append("upgrade from the release lost listed files")

        py = _venv(Path(tmp) / "alone")
        _pip(py, str(api), "httpx")
        modules = ",".join(check_api_surface.module_of(f) for f in listed)
        # cwd outside the repo: ``python -c`` puts the cwd on sys.path, and the
        # source tree there would hide what the wheel is missing.
        done = subprocess.run(
            [str(py), "-c", _ALONE, modules, str(args.max_rss_mb)], cwd=tmp
        )
        if done.returncode:
            failures.append("api-only install failed")

    for failure in failures:
        print(f"::error::{failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
