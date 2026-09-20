"""``server_revision`` — the three-part build identity, and its stamping.

The property the whole design rests on is that ``app_source_digest`` is
computable *identically* from the installed distribution and from the app repo's
source tree. Everything else is scaffolding around proving that, so these tests
build both sides for real — a source checkout and a PEP-376 ``dist-info`` with a
genuine ``RECORD`` — rather than mocking either.

No HTTP client is used. ``starlette.testclient`` needs httpx, which is not a
dependency of this SDK and must not become one, so the response tests drive the
ASGI callable directly. That is also strictly more truthful for the 500 case:
``ServerErrorMiddleware`` re-raises *after* sending, and driving ASGI ourselves
lets us assert the stamped response really went out on the wire before the
re-raise, which a test client would hide.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest

from server_sdk import revision as rev
from server_sdk.handler.base import DefaultHandler
from server_sdk.revision import (
    ServerRevision,
    compute_server_revision,
    declared_server_sdk_rev,
    environment_digest,
    header_safe,
    overlays_from_force_include,
    server_revision,
    source_digest_from_record,
    source_digest_from_tree,
)
from server_sdk.server import (
    APP_VERSION_HEADER,
    SERVER_REVISION_HEADER,
    build_asgi_app,
)

import importlib.metadata as importlib_metadata


# ===========================================================================
# Fixtures / builders
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolate_caches_and_path() -> Iterator[None]:
    """Each test gets a clean digest cache and its own sys.path edits undone.

    ``environment_digest`` is cached too (once per process, by design), and
    these tests move the installed closure around under it — so it is cleared
    here alongside the other two rather than leaking one test's closure into
    the next.
    """
    original_path = list(sys.path)
    rev.server_revision.cache_clear()
    rev._packages_to_distributions.cache_clear()
    rev.environment_digest.cache_clear()
    yield
    sys.path[:] = original_path
    importlib.invalidate_caches()
    rev.server_revision.cache_clear()
    rev._packages_to_distributions.cache_clear()
    rev.environment_digest.cache_clear()


def write_tree(root: Path, files: dict[str, str]) -> Path:
    """Write ``{relative path: text}`` under ``root``. Returns ``root``."""
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text))
    return root


def record_hash(data: bytes) -> str:
    """A PEP 376 RECORD hash cell: unpadded url-safe base64 of the sha256."""
    return (
        "sha256="
        + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    )


DEFAULT_REQUIRES = [
    "atlan-server-sdk[aws,sql] @ git+https://github.com/atlanhq/server-sdk.git"
    "@94ece49c20aff3c8d4bded646616f4b32649aa8e",
    "psycopg2-binary>=2.9.9",
    "sqlalchemy-redshift>=0.8.14",
    "atlan-server-sdk[workflow] @ git+https://github.com/atlanhq/server-sdk.git"
    "@94ece49c20aff3c8d4bded646616f4b32649aa8e ; extra == 'workflow'",
]


def install_into_site(
    site: Path,
    *,
    package: str,
    dist_name: str,
    version: str = "0.1.0",
    source: Path,
    extra_installed: dict[str, str] | None = None,
    requires: list[str] | None = None,
    record_package_files: bool = True,
    copy_package_files: bool = True,
    record_subtree: str | None = None,
) -> Path:
    """Materialize a wheel-style install of ``source`` into ``site``.

    Copies the package, then writes a real ``dist-info`` with a METADATA and a
    RECORD whose hash cells are computed over the installed bytes — the same
    thing ``uv pip install`` leaves behind, which is what the RECORD reader has
    to cope with in production.

    ``record_package_files=False`` reproduces an **editable** install: the
    package is not installed, only reached through a ``.pth`` shim.

    ``copy_package_files=False`` + ``record_subtree=`` together reproduce the
    editable install of a **force-include** app, which is the shape that matters
    and the one the plain editable case does not cover. Verified against a dev
    venv: ``site-packages/redshift_server/`` holds *only* ``generated/``, the
    ``.pth`` names ``atlan-redshift-app/server``, and
    ``atlan_redshift_server-0.1.0.dist-info/RECORD`` lists the ``.pth``, the
    dist-info, and ``redshift_server/generated/**`` — ``.py`` files included —
    and no ``redshift_server/__init__.py``.
    """
    site.mkdir(parents=True, exist_ok=True)
    installed_pkg = site / package
    for src in sorted(source.rglob("*")) if copy_package_files else []:
        if not src.is_file():
            continue
        dst = installed_pkg / src.relative_to(source)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    for rel, text in (extra_installed or {}).items():
        dst = installed_pkg / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(textwrap.dedent(text))

    dist_info = site / f"{dist_name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    metadata_lines = [
        "Metadata-Version: 2.5",
        f"Name: {dist_name}",
        f"Version: {version}",
        "Requires-Python: <4.0,>=3.11",
    ]
    metadata_lines += [
        f"Requires-Dist: {r}"
        for r in (DEFAULT_REQUIRES if requires is None else requires)
    ]
    metadata = "\n".join(metadata_lines) + "\n"
    (dist_info / "METADATA").write_text(metadata)
    (dist_info / "WHEEL").write_text("Wheel-Version: 1.0\nGenerator: test\n")

    rows: list[str] = []
    if record_package_files:
        for installed in sorted(installed_pkg.rglob("*")):
            if not installed.is_file():
                continue
            data = installed.read_bytes()
            rel = installed.relative_to(site).as_posix()
            rows.append(f"{rel},{record_hash(data)},{len(data)}")
    else:
        pth = site / f"_editable_impl_{dist_name.replace('-', '_')}.pth"
        pth.write_text(str(source.parent) + "\n")
        data = pth.read_bytes()
        rows.append(f"{pth.name},{record_hash(data)},{len(data)}")
        if record_subtree is not None:
            for installed in sorted((installed_pkg / record_subtree).rglob("*")):
                if not installed.is_file():
                    continue
                data = installed.read_bytes()
                rel = installed.relative_to(site).as_posix()
                rows.append(f"{rel},{record_hash(data)},{len(data)}")
    meta_bytes = (dist_info / "METADATA").read_bytes()
    rows.append(
        f"{dist_info.name}/METADATA,{record_hash(meta_bytes)},{len(meta_bytes)}"
    )
    rows.append(f"{dist_info.name}/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n")
    return dist_info


APP_SOURCE = {
    "__init__.py": '''
        """Acme server package."""

        VERSION_MARKER = "alpha"
    ''',
    "client.py": """
        def connect(dsn: str) -> str:
            return dsn
    """,
    "handlers/__init__.py": "",
    "handlers/auth.py": """
        async def test_auth(payload: dict) -> dict:
            return {"status": "success"}
    """,
    "py.typed": "",
    "README.md": "not source\n",
    "data/values.json": '{"a": 1}\n',
}


# ===========================================================================
# Raw-ASGI driver
# ===========================================================================


class Captured:
    """What an ASGI app actually put on the wire, plus anything it re-raised."""

    def __init__(self, messages: list[dict[str, Any]], raised: BaseException | None):
        start = next((m for m in messages if m["type"] == "http.response.start"), None)
        assert start is not None, "app sent no http.response.start"
        self.status: int = start["status"]
        self.headers: dict[str, str] = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in start["headers"]
        }
        self.raw_headers: list[tuple[bytes, bytes]] = list(start["headers"])
        self.body: bytes = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        self.raised = raised

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def revision_header(self) -> str:
        return self.headers[SERVER_REVISION_HEADER.lower()]

    @property
    def version_header(self) -> str:
        return self.headers[APP_VERSION_HEADER.lower()]


def call_asgi(
    app: Any,
    path: str = "/",
    *,
    method: str = "GET",
    query: str = "",
    body: bytes = b"",
) -> Captured:
    messages: list[dict[str, Any]] = []
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": [
            (b"host", b"acme.tenant.example"),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 51000),
        "server": ("acme.tenant.example", 80),
    }
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    raised: BaseException | None = None

    async def run() -> None:
        nonlocal raised
        try:
            await app(scope, receive, send)
        except BaseException as exc:  # ServerErrorMiddleware re-raises after sending
            raised = exc

    asyncio.run(run())
    return Captured(messages, raised)


# ===========================================================================
# app_source_digest — dist side == source side
# ===========================================================================


def test_installed_dist_digest_equals_source_tree_digest(tmp_path: Path) -> None:
    """The load-bearing property: RECORD and a checkout agree, bit for bit."""
    source = write_tree(tmp_path / "repo" / "server" / "acme_server", APP_SOURCE)
    dist_info = install_into_site(
        tmp_path / "site",
        package="acme_server",
        dist_name="atlan-acme-server",
        source=source,
    )

    from_dist = source_digest_from_record(
        importlib_metadata.PathDistribution(dist_info), "acme_server"
    )
    from_source = source_digest_from_tree(source)

    assert from_dist is not None
    assert from_source is not None
    assert from_dist == from_source
    assert len(from_dist) == rev.DIGEST_LEN
    assert set(from_dist) <= set("0123456789abcdef")


def test_digest_matches_end_to_end_through_compute(tmp_path: Path) -> None:
    """The public entry point resolves the dist and lands on the same digest."""
    source = write_tree(tmp_path / "repo" / "server" / "acme_e2e", APP_SOURCE)
    site = tmp_path / "site"
    install_into_site(
        site, package="acme_e2e", dist_name="atlan-acme-e2e", source=source
    )
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()

    revision = compute_server_revision("acme_e2e", dist_name="atlan-acme-e2e")

    assert revision.app_source_digest == source_digest_from_tree(source)
    assert revision.app_source_digest is not None


def test_overlays_reproduce_a_force_include_install(tmp_path: Path) -> None:
    """A checkout can still match a wheel that grafts files in at build time.

    ``atlan-redshift-app/server/pyproject.toml`` declares
    ``force-include = { "../app/generated" = "redshift_server/generated" }``, so
    the installed package holds ``.py`` files the package directory in the repo
    does not. ``overlays`` is how the repo side reproduces that mapping.
    """
    source = write_tree(tmp_path / "repo" / "server" / "acme_fi", APP_SOURCE)
    app_generated = write_tree(
        tmp_path / "repo" / "app" / "generated",
        {
            "__init__.py": "",
            "crawler/__init__.py": "",
            "crawler/_input.py": "FIELDS = ('include', 'exclude')\n",
            "crawler/manifest.json": '{"dag": {}}\n',
        },
    )
    install_into_site(
        tmp_path / "site",
        package="acme_fi",
        dist_name="atlan-acme-fi",
        source=source,
        extra_installed={
            "generated/__init__.py": "",
            "generated/crawler/__init__.py": "",
            "generated/crawler/_input.py": "FIELDS = ('include', 'exclude')\n",
            "generated/crawler/manifest.json": '{"dag": {}}\n',
        },
    )
    dist_info = next((tmp_path / "site").glob("*.dist-info"))

    from_dist = source_digest_from_record(
        importlib_metadata.PathDistribution(dist_info), "acme_fi"
    )
    assert from_dist is not None
    # Without the overlay the two sides legitimately disagree...
    assert from_dist != source_digest_from_tree(source)
    # ...and with it they agree.
    assert from_dist == source_digest_from_tree(
        source, overlays={"generated": app_generated}
    )


def test_editable_install_falls_back_to_the_resolved_tree(tmp_path: Path) -> None:
    """An editable RECORD carries no source, so the on-disk tree answers instead."""
    source = write_tree(tmp_path / "repo" / "server" / "acme_edit", APP_SOURCE)
    site = tmp_path / "site"
    install_into_site(
        site,
        package="acme_edit",
        dist_name="atlan-acme-edit",
        source=source,
        record_package_files=False,
    )
    dist_info = next(site.glob("*.dist-info"))
    dist = importlib_metadata.PathDistribution(dist_info)

    # RECORD alone cannot answer for an editable install.
    assert source_digest_from_record(dist, "acme_edit") is None

    # But the package still resolves, and its tree gives the same digest the
    # wheel install would have produced.
    sys.path.insert(0, str(site))
    sys.path.insert(0, str(source.parent))
    importlib.invalidate_caches()
    revision = compute_server_revision("acme_edit", dist_name="atlan-acme-edit")
    assert revision.app_source_digest == source_digest_from_tree(source)


def _editable_force_include_install(
    tmp_path: Path, name: str, dist_name: str
) -> tuple[Path, Path, Path]:
    """An editable install of a force-include app: checkout, site, dist-info.

    Mirrors what ``uv pip install -e`` leaves behind for redshift / snowflake /
    governance — see :func:`install_into_site`.
    """
    source = write_tree(tmp_path / name / "repo" / "server" / name, APP_SOURCE)
    app_generated = write_tree(
        tmp_path / name / "repo" / "app" / "generated",
        {
            "__init__.py": "",
            "crawler/__init__.py": "",
            "crawler/_input.py": "FIELDS = ('include', 'exclude')\n",
            "crawler/manifest.json": '{"dag": {}}\n',
        },
    )
    site = tmp_path / name / "site"
    dist_info = install_into_site(
        site,
        package=name,
        dist_name=dist_name,
        version="4.2.0",
        source=source,
        copy_package_files=False,
        record_package_files=False,
        record_subtree="generated",
        extra_installed={
            "generated/__init__.py": "",
            "generated/crawler/__init__.py": "",
            "generated/crawler/_input.py": "FIELDS = ('include', 'exclude')\n",
            "generated/crawler/manifest.json": '{"dag": {}}\n',
        },
    )
    return source, app_generated, dist_info


def test_editable_record_listing_only_the_overlay_is_rejected(tmp_path: Path) -> None:
    """The RECORD path must not trust a RECORD that describes only the graft.

    This is the case the old ``if not pairs`` guard missed. An editable install
    of a force-include app leaves a RECORD that *is* non-empty for the package —
    it lists ``<pkg>/generated/**``, ``.py`` files included — while listing none
    of the app's own server modules. Trusting it returned a digest computed over
    the overlay alone: a value describing almost nothing, which can never equal
    the wheel's digest, and which the documented fallback was supposed to
    prevent ever being produced.
    """
    source, app_generated, dist_info = _editable_force_include_install(
        tmp_path, "acme_edov", "atlan-acme-edov"
    )
    dist = importlib_metadata.PathDistribution(dist_info)
    record = dist.read_text("RECORD") or ""

    # The premise: RECORD really does carry .py rows for this package...
    assert "acme_edov/generated/crawler/_input.py" in record
    # ...and really does not carry the package's own top-level __init__.py.
    assert "\nacme_edov/__init__.py," not in "\n" + record

    # So it is not authoritative, and is rejected rather than digested.
    assert source_digest_from_record(dist, "acme_edov") is None

    # And what it *would* have returned is a digest of the overlay alone —
    # the value this guard exists to keep out of a header.
    overlay_only = source_digest_from_tree(app_generated)
    assert overlay_only is not None
    assert overlay_only != source_digest_from_tree(source)


def test_editable_force_include_install_falls_back_to_the_checkout(
    tmp_path: Path,
) -> None:
    """Rejecting the RECORD is only useful if the fallback then answers."""
    source, _app_generated, _dist_info = _editable_force_include_install(
        tmp_path, "acme_edfb", "atlan-acme-edfb"
    )
    # site first, exactly as a venv orders it: site-packages/acme_edfb/ exists
    # but holds no __init__.py, so it is only a namespace portion and the real
    # regular package behind the .pth wins.
    sys.path.insert(0, str(tmp_path / "acme_edfb" / "site"))
    sys.path.insert(0, str(source.parent))
    importlib.invalidate_caches()

    revision = compute_server_revision("acme_edfb", dist_name="atlan-acme-edfb")
    assert revision.app_source_digest == source_digest_from_tree(source)
    assert revision.app_source_digest is not None


def test_overlay_only_records_would_have_collided(tmp_path: Path) -> None:
    """Why the guard is not cosmetic: two apps, one digest.

    Two different apps sharing a generated tree (the same contract version, the
    normal case for two connectors generated by the same toolchain) had
    *identical* overlay-only digests while their actual server code differed.
    With the guard both fall back and become distinguishable again — which is
    the entire premise of stamping in a consolidated host.
    """
    shared_generated = {
        "__init__.py": "",
        "crawler/__init__.py": "",
        "crawler/_input.py": "FIELDS = ('include', 'exclude')\n",
    }
    left_files = dict(APP_SOURCE)
    right_files = dict(APP_SOURCE, **{"client.py": "def connect(dsn):\n    return 1\n"})

    digests = []
    for name, files in (("acme_coll_l", left_files), ("acme_coll_r", right_files)):
        source = write_tree(tmp_path / name / "repo" / name, files)
        write_tree(tmp_path / name / "repo" / "generated", shared_generated)
        site = tmp_path / name / "site"
        dist_info = install_into_site(
            site,
            package=name,
            dist_name=f"atlan-{name.replace('_', '-')}",
            source=source,
            copy_package_files=False,
            record_package_files=False,
            record_subtree="generated",
            extra_installed={
                f"generated/{rel}": text for rel, text in shared_generated.items()
            },
        )
        dist = importlib_metadata.PathDistribution(dist_info)
        assert source_digest_from_record(dist, name) is None
        digests.append(source_digest_from_tree(source))

    # The overlay is byte-identical for both, so an overlay-only digest could
    # only ever have been the same value twice. The checkouts are not.
    assert digests[0] != digests[1]
    assert None not in digests


def test_wheel_record_is_still_trusted(tmp_path: Path) -> None:
    """The guard must discriminate, not just reject: a wheel RECORD passes."""
    source = write_tree(tmp_path / "repo" / "acme_wheel", APP_SOURCE)
    dist_info = install_into_site(
        tmp_path / "site",
        package="acme_wheel",
        dist_name="atlan-acme-wheel",
        source=source,
    )
    dist = importlib_metadata.PathDistribution(dist_info)
    record = dist.read_text("RECORD") or ""

    assert "acme_wheel/__init__.py" in record
    from_record = source_digest_from_record(dist, "acme_wheel")
    assert from_record is not None
    assert from_record == source_digest_from_tree(source)


# ===========================================================================
# overlays_from_force_include — derive, do not hand-write
# ===========================================================================


def test_overlays_derived_for_a_directory_graft(tmp_path: Path) -> None:
    """redshift / snowflake / governance: a directory carrying .py files."""
    repo = tmp_path / "repo"
    write_tree(repo / "server" / "redshift_server", APP_SOURCE)
    generated = write_tree(
        repo / "app" / "generated",
        {"__init__.py": "", "crawler/_input.py": "FIELDS = ()\n"},
    )

    derived = overlays_from_force_include(
        {"../app/generated": "redshift_server/generated"},
        base=repo / "server",
        package="redshift_server",
    )
    assert set(derived) == {"generated"}
    assert derived["generated"].resolve() == generated.resolve()

    # And it is the mapping that actually makes the two sides agree.
    dist_info = install_into_site(
        tmp_path / "site",
        package="redshift_server",
        dist_name="atlan-redshift-server",
        source=repo / "server" / "redshift_server",
        extra_installed={
            "generated/__init__.py": "",
            "generated/crawler/_input.py": "FIELDS = ()\n",
        },
    )
    from_record = source_digest_from_record(
        importlib_metadata.PathDistribution(dist_info), "redshift_server"
    )
    assert from_record == source_digest_from_tree(
        repo / "server" / "redshift_server", overlays=derived
    )


def test_overlays_derived_for_a_json_only_graft(tmp_path: Path) -> None:
    """atlan-memory-app: two grafted .json files, each overlaid by name.

    ARUN-942 regression. These files are served at request time, so they must
    move the digest; when they did not, an edit to gov's role-sync manifest left
    ``server_revision`` unchanged, ``bump-app-pin`` reported "unchanged", and the
    host served the stale manifest indefinitely.

    Handing the enclosing *directory* over instead — the obvious generalization
    of the redshift recipe — still folds in files that never reach that app's
    wheel and produces a repo-side digest that can never match the image.
    """
    repo = tmp_path / "repo"
    package_root = write_tree(repo / "server" / "memory_server", APP_SOURCE)
    write_tree(
        repo / "app" / "generated",
        {
            "a.json": '{"a": 1}\n',
            "b.json": '{"b": 2}\n',
            # Sits in the same directory and is NOT force-included.
            "_input.py": "NOT_IN_THE_WHEEL = True\n",
        },
    )

    derived = overlays_from_force_include(
        {
            "../app/generated/a.json": "memory_server/generated/a.json",
            "../app/generated/b.json": "memory_server/generated/b.json",
        },
        base=repo / "server",
        package="memory_server",
    )
    assert set(derived) == {"generated/a.json", "generated/b.json"}
    assert (
        derived["generated/a.json"].resolve()
        == (repo / "app" / "generated" / "a.json").resolve()
    )
    assert (
        derived["generated/b.json"].resolve()
        == (repo / "app" / "generated" / "b.json").resolve()
    )

    # The wheel carries the two .json files, so the correct repo-side digest is
    # the package plus exactly those two — which is what the derived overlay
    # produces...
    dist_info = install_into_site(
        tmp_path / "site",
        package="memory_server",
        dist_name="atlan-memory-server",
        source=package_root,
        extra_installed={
            "generated/a.json": '{"a": 1}\n',
            "generated/b.json": '{"b": 2}\n',
        },
    )
    from_record = source_digest_from_record(
        importlib_metadata.PathDistribution(dist_info), "memory_server"
    )
    assert from_record == source_digest_from_tree(package_root, overlays=derived)

    # ...the bare package is NOT it: the grafted manifests are part of what the
    # image serves, so omitting them is the stale-manifest bug itself.
    assert from_record != source_digest_from_tree(package_root)

    # ...and the directory shortcut is still exactly the wrong answer: it folds
    # in _input.py, which this app's wheel never carries.
    assert from_record != source_digest_from_tree(
        package_root, overlays={"generated": repo / "app" / "generated"}
    )

    # Editing one grafted manifest must move the digest — the whole point.
    (repo / "app" / "generated" / "a.json").write_text('{"a": 2}\n')
    assert source_digest_from_tree(package_root, overlays=derived) != from_record


def test_overlays_ignore_grafts_landing_outside_the_package(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_tree(repo / "server" / "acme_out", APP_SOURCE)
    write_tree(repo / "shared", {"util.py": "X = 1\n"})
    assert (
        overlays_from_force_include(
            {"../shared": "some_other_package/shared"},
            base=repo / "server",
            package="acme_out",
        )
        == {}
    )


def test_single_source_file_overlay_moves_the_digest(tmp_path: Path) -> None:
    """A one-.py-file graft is a real shape; the overlay must carry it."""
    repo = tmp_path / "repo"
    package_root = write_tree(repo / "server" / "acme_one", APP_SOURCE)
    grafted = repo / "gen" / "_input.py"
    grafted.parent.mkdir(parents=True, exist_ok=True)
    grafted.write_text("FIELDS = ()\n")

    derived = overlays_from_force_include(
        {"../gen/_input.py": "acme_one/generated/_input.py"},
        base=repo / "server",
        package="acme_one",
    )
    assert set(derived) == {"generated/_input.py"}

    dist_info = install_into_site(
        tmp_path / "site",
        package="acme_one",
        dist_name="atlan-acme-one",
        source=package_root,
        extra_installed={"generated/_input.py": "FIELDS = ()\n"},
    )
    from_record = source_digest_from_record(
        importlib_metadata.PathDistribution(dist_info), "acme_one"
    )
    assert from_record is not None
    assert from_record != source_digest_from_tree(package_root)
    assert from_record == source_digest_from_tree(package_root, overlays=derived)


# ===========================================================================
# app_source_digest — stability and sensitivity
# ===========================================================================


def test_digest_is_stable_across_repeated_calls(tmp_path: Path) -> None:
    source = write_tree(tmp_path / "acme_stable", APP_SOURCE)
    digests = {source_digest_from_tree(source) for _ in range(10)}
    assert len(digests) == 1
    assert digests != {None}


def test_digest_is_stable_across_process_restarts(tmp_path: Path) -> None:
    """Two fresh interpreters, no shared state, same answer as this process."""
    source = write_tree(tmp_path / "acme_restart", APP_SOURCE)
    script = (
        "import sys;"
        "from server_sdk.revision import source_digest_from_tree;"
        f"print(source_digest_from_tree({str(source)!r}))"
    )
    runs = [
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(tmp_path),
        ).stdout.strip()
        for _ in range(2)
    ]
    assert runs[0] == runs[1]
    assert runs[0] == source_digest_from_tree(source)
    assert runs[0] != "None"


def test_digest_ignores_mtime_and_permissions(tmp_path: Path) -> None:
    source = write_tree(tmp_path / "acme_meta", APP_SOURCE)
    before = source_digest_from_tree(source)

    for path in sorted(source.rglob("*")):
        if path.is_file():
            os.utime(path, (1_000_000, 1_000_000))
            os.chmod(path, 0o600)
    after_touch = source_digest_from_tree(source)

    for path in sorted(source.rglob("*")):
        if path.is_file():
            os.utime(path, (2_000_000_000, 2_000_000_000))
            os.chmod(path, 0o644)
    after_again = source_digest_from_tree(source)

    assert before == after_touch == after_again
    assert before is not None


def test_digest_changes_when_a_single_source_byte_changes(tmp_path: Path) -> None:
    source = write_tree(tmp_path / "acme_byte", APP_SOURCE)
    before = source_digest_from_tree(source)

    target = source / "client.py"
    data = bytearray(target.read_bytes())
    data[-2] = data[-2] ^ 0x20  # flip case of one character
    target.write_bytes(bytes(data))

    after = source_digest_from_tree(source)
    assert before is not None
    assert after is not None
    assert before != after


def test_digest_changes_when_a_source_file_is_renamed(tmp_path: Path) -> None:
    """Content-only hashing would miss this; the digest folds paths in too."""
    source = write_tree(tmp_path / "acme_rename", APP_SOURCE)
    before = source_digest_from_tree(source)
    (source / "client.py").rename(source / "clients.py")
    assert source_digest_from_tree(source) != before


def test_digest_ignores_prose_and_pycache(tmp_path: Path) -> None:
    """Prose and interpreter output stay outside the digest.

    ``__pycache__`` in particular: it exists in a container layer but never in
    RECORD with a usable hash, so counting it would make the two sides disagree
    by construction.
    """
    source = write_tree(tmp_path / "acme_scope", APP_SOURCE)
    before = source_digest_from_tree(source)

    (source / "README.md").write_text("rewritten\n")
    (source / "py.typed").write_text("# still not source\n")
    pycache = source / "__pycache__"
    pycache.mkdir(exist_ok=True)
    (pycache / "client.cpython-311.pyc").write_bytes(b"\x00\x01compiled")
    (pycache / "shadow.py").write_text("SHOULD_NOT_COUNT = True\n")

    assert source_digest_from_tree(source) == before


def test_digest_tracks_served_data_files(tmp_path: Path) -> None:
    """ARUN-942: .json/.yaml ship in the package and are served, so they count.

    Each is asserted separately — a single combined edit would still pass if
    only one suffix were wired up.
    """
    source = write_tree(tmp_path / "acme_data", APP_SOURCE)

    before = source_digest_from_tree(source)
    (source / "data" / "values.json").write_text('{"a": 2}\n')
    after_json = source_digest_from_tree(source)
    assert after_json != before

    (source / "data" / "extra.yaml").write_text("k: v\n")
    after_yaml = source_digest_from_tree(source)
    assert after_yaml != after_json

    (source / "data" / "extra.yml").write_text("k: w\n")
    assert source_digest_from_tree(source) != after_yaml


def test_digest_is_none_for_a_tree_with_no_source(tmp_path: Path) -> None:
    """Explicit unknown, never a digest-of-nothing that two apps would share."""
    empty = write_tree(tmp_path / "empty_pkg", {"README.md": "hi\n"})
    assert source_digest_from_tree(empty) is None
    assert source_digest_from_tree(tmp_path / "does-not-exist") is None


# ===========================================================================
# server_sdk_declared_rev
# ===========================================================================


def _dist_with_requires(tmp_path: Path, requires: list[str], name: str = "acme-req"):
    source = write_tree(tmp_path / name / "pkg", {"__init__.py": "x = 1\n"})
    dist_info = install_into_site(
        tmp_path / name / "site",
        package="pkg",
        dist_name=name,
        source=source,
        requires=requires,
    )
    return importlib_metadata.PathDistribution(dist_info)


def test_declared_rev_parses_the_pinned_direct_reference(tmp_path: Path) -> None:
    dist = _dist_with_requires(tmp_path, DEFAULT_REQUIRES)
    declared = declared_server_sdk_rev(dist)
    assert declared == (
        "atlan-server-sdk[aws,sql] @ git+https://github.com/atlanhq/server-sdk.git"
        "@94ece49c20aff3c8d4bded646616f4b32649aa8e"
    )
    # The marker-gated [workflow] pin exists in the same METADATA and must not
    # be the one reported: it is not what the base install resolves.
    assert "extra ==" not in declared


def test_declared_rev_normalizes_the_requirement_name(tmp_path: Path) -> None:
    """PEP 503: ``Atlan_Server.SDK`` is the same project as ``atlan-server-sdk``."""
    dist = _dist_with_requires(
        tmp_path,
        ["Atlan_Server.SDK>=0.1.0,<1.0.0", "orjson>=3.10.0"],
        name="acme-norm",
    )
    assert declared_server_sdk_rev(dist) == "Atlan_Server.SDK>=0.1.0,<1.0.0"


def test_declared_rev_falls_back_to_a_marker_gated_entry(tmp_path: Path) -> None:
    dist = _dist_with_requires(
        tmp_path,
        [
            "orjson>=3.10.0",
            "atlan-server-sdk[workflow] @ git+https://github.com/atlanhq/"
            "server-sdk.git@deadbeef ; extra == 'workflow'",
        ],
        name="acme-marker",
    )
    declared = declared_server_sdk_rev(dist)
    assert declared is not None
    assert declared.startswith("atlan-server-sdk[workflow] @ git+")


def test_declared_rev_is_none_when_the_sdk_is_not_required(tmp_path: Path) -> None:
    dist = _dist_with_requires(
        tmp_path, ["orjson>=3.10.0", "fastapi>=0.115.0"], name="acme-nosdk"
    )
    assert declared_server_sdk_rev(dist) is None


def test_declared_rev_degrades_when_the_dist_is_absent() -> None:
    """No metadata anywhere: a degraded stamp, not an exception."""
    assert declared_server_sdk_rev(None) is None

    revision = compute_server_revision(
        "no_such_package_xyz", dist_name="no-such-distribution-xyz"
    )
    assert isinstance(revision, ServerRevision)
    assert revision.app_source_digest is None
    assert revision.server_sdk_declared_rev is None
    # env_digest is host-side and still answerable.
    assert revision.env_digest is not None
    assert revision.as_header() == (
        f"src={rev.UNKNOWN};sdk={rev.UNKNOWN};env={revision.env_digest}"
    )


def test_revision_with_no_package_is_fully_unknown() -> None:
    """An app that has not adopted the stamp still serves; nothing raises."""
    revision = compute_server_revision(None)
    assert revision.app_source_digest is None
    assert revision.server_sdk_declared_rev is None


# ===========================================================================
# env_digest
# ===========================================================================


def test_env_digest_is_stable_and_short() -> None:
    first = environment_digest()
    assert first is not None
    assert first == environment_digest()
    assert len(first) == rev.DIGEST_LEN


def test_env_digest_moves_when_the_closure_moves(tmp_path: Path) -> None:
    """Forensic value depends on it actually tracking the installed set."""
    before = environment_digest()
    site = tmp_path / "extra-site"
    install_into_site(
        site,
        package="acme_envmove",
        dist_name="atlan-acme-envmove",
        version="9.9.9",
        source=write_tree(tmp_path / "src" / "acme_envmove", {"__init__.py": ""}),
    )
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    # environment_digest is cached for the process lifetime; only a test moves
    # the closure under a live interpreter, so only a test has to say so.
    environment_digest.cache_clear()
    assert environment_digest() != before


# ===========================================================================
# No environment is read for identity
# ===========================================================================


class _ExplodingEnviron(MutableMapping):
    """Any read at all is a test failure."""

    def _boom(self, key: object = None) -> None:
        raise AssertionError(
            f"build identity must not read process env (touched {key!r})"
        )

    def __getitem__(self, key: str) -> str:
        self._boom(key)
        raise KeyError(key)  # pragma: no cover

    def __setitem__(self, key: str, value: str) -> None:
        self._boom(key)

    def __delitem__(self, key: str) -> None:
        self._boom(key)

    def __iter__(self) -> Any:
        self._boom()
        raise StopIteration  # pragma: no cover

    def __len__(self) -> int:
        self._boom()
        return 0  # pragma: no cover

    def __contains__(self, key: object) -> bool:
        self._boom(key)
        return False  # pragma: no cover

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        self._boom(key)


def test_identity_never_reads_process_env(tmp_path: Path) -> None:
    """Identity comes from the passed-in package, never from the environment.

    This is not a style preference. In the consolidated host
    ``ATLAN_APPLICATION_NAME`` is ``common-app-server`` for every hosted app, so
    an env-derived identity would stamp all five with the host's name — the same
    defect already found three times in the manifest task-queue path.

    ``os.environ`` is swapped by hand rather than with ``monkeypatch`` so the
    real mapping is restored inside this function: pytest's own terminal writer
    reads ``COLUMNS``/``PY_COLORS``, and leaving the trap armed for even one
    fixture-teardown hook takes the whole session down with it.
    """
    source = write_tree(tmp_path / "repo" / "acme_noenv", APP_SOURCE)
    site = tmp_path / "site"
    install_into_site(
        site, package="acme_noenv", dist_name="atlan-acme-noenv", source=source
    )
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    expected = source_digest_from_tree(source)

    real_environ = os.environ
    os.environ = _ExplodingEnviron()  # type: ignore[assignment]
    try:
        revision = compute_server_revision("acme_noenv", dist_name="atlan-acme-noenv")
        tree_digest = source_digest_from_tree(source)
        record_digest = rev.app_source_digest(
            "acme_noenv", rev.find_distribution("acme_noenv", "atlan-acme-noenv")
        )
        env_digest_value = environment_digest()
    finally:
        os.environ = real_environ  # type: ignore[assignment]

    assert revision.app_source_digest == expected
    assert revision.server_sdk_declared_rev is not None
    assert revision.env_digest is not None
    assert tree_digest == expected
    assert record_digest == expected
    assert env_digest_value is not None


def test_revision_module_contains_no_env_lookup() -> None:
    """Belt and braces: the source itself names no env accessor."""
    module_source = Path(rev.__file__).read_text()
    code = "\n".join(
        line for line in module_source.splitlines() if not line.strip().startswith("#")
    )
    body = code.split('"""', 2)[-1]  # drop the module docstring
    for forbidden in ("os.environ", "os.getenv", "environb", "getenv("):
        assert forbidden not in body, f"revision.py must not use {forbidden}"


# ===========================================================================
# Caching
# ===========================================================================


def test_cached_accessor_returns_the_same_object(tmp_path: Path) -> None:
    source = write_tree(tmp_path / "repo" / "acme_cache", APP_SOURCE)
    site = tmp_path / "site"
    install_into_site(
        site, package="acme_cache", dist_name="atlan-acme-cache", source=source
    )
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()

    first = server_revision("acme_cache", "atlan-acme-cache")
    second = server_revision("acme_cache", "atlan-acme-cache")
    assert first is second
    assert first == compute_server_revision("acme_cache", dist_name="atlan-acme-cache")


# ===========================================================================
# Header stamping on every route
# ===========================================================================


def install_app_package(
    tmp_path: Path, name: str, files: dict[str, str] | None = None
) -> Path:
    """Install ``name`` as a wheel-style dist and put it on ``sys.path``."""
    source = write_tree(tmp_path / "repo" / name, files or APP_SOURCE)
    site = tmp_path / f"site-{name}"
    install_into_site(
        site, package=name, dist_name=f"atlan-{name}", version="4.2.0", source=source
    )
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    return source


def make_app(tmp_path: Path, name: str, generated_dir: Path | None = None):
    return build_asgi_app(
        DefaultHandler(),
        app_name=name.replace("_", "-"),
        generated_dir=generated_dir or (tmp_path / "no-such-generated"),
        app_package=name,
        app_dist=f"atlan-{name}",
    )


def build_stamped_app(
    tmp_path: Path,
    name: str,
    *,
    files: dict[str, str] | None = None,
    generated_dir: Path | None = None,
):
    """An installed app package plus the FastAPI app that serves it."""
    source = install_app_package(tmp_path, name, files)
    return make_app(tmp_path, name, generated_dir), source


def assert_stamped(response: Captured, expected_digest: str | None = None) -> None:
    assert response.version_header == "4.2.0"
    header = response.revision_header
    assert header.startswith("src=")
    assert ";sdk=" in header and ";env=" in header
    assert header.isascii()
    if expected_digest is not None:
        assert header.split(";")[0] == f"src={expected_digest}"


def test_headers_on_a_200(tmp_path: Path) -> None:
    app, source = build_stamped_app(tmp_path, "acme_h200")
    response = call_asgi(app, "/server/health")
    assert response.status == 200
    assert response.json() == {"status": "ok"}
    assert_stamped(response, source_digest_from_tree(source))


def test_headers_on_a_404(tmp_path: Path) -> None:
    app, source = build_stamped_app(tmp_path, "acme_h404")
    response = call_asgi(app, "/definitely/not/a/route")
    assert response.status == 404
    assert_stamped(response, source_digest_from_tree(source))


def test_headers_on_a_405(tmp_path: Path) -> None:
    app, source = build_stamped_app(tmp_path, "acme_h405")
    # /server/health is GET-only; Starlette answers a POST with 405 itself.
    response = call_asgi(app, "/server/health", method="POST")
    assert response.status == 405
    assert_stamped(response, source_digest_from_tree(source))

    # The explicit POST /manifest 405 heracles depends on is stamped too.
    manifest_405 = call_asgi(app, "/manifest", method="POST", body=b"{}")
    assert manifest_405.status == 405
    assert manifest_405.headers["allow"] == "GET"
    assert_stamped(manifest_405, source_digest_from_tree(source))


def test_headers_on_a_500_from_a_raising_handler(tmp_path: Path) -> None:
    """The one path a user middleware cannot reach, covered by the 500 handler."""
    app, source = build_stamped_app(tmp_path, "acme_h500")

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("detonation in the handler")

    response = call_asgi(app, "/boom")
    assert response.status == 500
    assert_stamped(response, source_digest_from_tree(source))
    # Generic body — no traceback, no internals leaked to the client.
    assert response.json() == {
        "success": False,
        "data": {},
        "message": "Internal server error",
    }
    assert b"detonation" not in response.body
    # ...and the exception still propagates so the process log keeps the trace.
    assert isinstance(response.raised, RuntimeError)


def test_headers_on_a_400_from_the_entrypoint_guard(tmp_path: Path) -> None:
    """A route-raised HTTPException is stamped without route cooperation."""
    app, source = build_stamped_app(tmp_path, "acme_h400")
    response = call_asgi(
        app,
        "/workflows/v1/auth",
        method="POST",
        body=json.dumps({"entrypoint": "../etc/passwd"}).encode(),
    )
    assert response.status == 400
    assert_stamped(response, source_digest_from_tree(source))


def test_app_supplied_header_is_not_overwritten(tmp_path: Path) -> None:
    app, _ = build_stamped_app(tmp_path, "acme_hown")

    from fastapi.responses import JSONResponse

    @app.get("/own")
    async def own() -> JSONResponse:
        return JSONResponse({"ok": True}, headers={APP_VERSION_HEADER: "pinned"})

    response = call_asgi(app, "/own")
    assert response.version_header == "pinned"
    # And it is present exactly once, not duplicated.
    names = [n.decode().lower() for n, _ in response.raw_headers]
    assert names.count(APP_VERSION_HEADER.lower()) == 1


def test_app_without_a_package_still_serves_and_stamps() -> None:
    """Adoption is optional: no app_package means 'unknown', not a failure."""
    app = build_asgi_app(DefaultHandler(), app_name="unadopted")
    response = call_asgi(app, "/server/health")
    assert response.status == 200
    assert response.version_header == rev.UNKNOWN
    assert response.revision_header.startswith(f"src={rev.UNKNOWN};sdk={rev.UNKNOWN};")


def test_header_value_is_sanitized_and_ascii() -> None:
    """A Requires-Dist string carries spaces, quotes and our own delimiters."""
    revision = ServerRevision(
        app_source_digest="c88ba2d7bfa5e0e6",
        server_sdk_declared_rev=(
            "atlan-server-sdk[workflow] @ git+https://github.com/atlanhq/"
            "server-sdk.git@deadbeef ; extra == 'workflow'"
        ),
        env_digest="0123456789abcdef",
    )
    header = revision.as_header()
    assert header.isascii()
    assert header.count(";") == 2  # exactly our two delimiters, none smuggled in
    assert "\n" not in header and "\r" not in header and " " not in header
    src, sdk, env = header.split(";")
    assert src == "src=c88ba2d7bfa5e0e6"
    assert env == "env=0123456789abcdef"
    assert sdk.startswith("sdk=atlan-server-sdk_workflow_")
    assert "git+https://github.com/atlanhq/server-sdk.git@deadbeef" in sdk


# ===========================================================================
# Two apps in one process — the whole point
# ===========================================================================


def test_two_sub_apps_in_one_process_report_different_digests(
    tmp_path: Path,
) -> None:
    """Consolidation's core requirement: one process, two distinguishable builds."""
    alpha_files = dict(APP_SOURCE)
    beta_files = dict(APP_SOURCE)
    beta_files["client.py"] = """
        def connect(dsn: str) -> str:
            return dsn.upper()
    """

    # Both installed before either app is built, so the two apps observe one
    # identical dependency closure — env_digest describes the process.
    alpha_source = install_app_package(tmp_path, "acme_alpha", alpha_files)
    beta_source = install_app_package(tmp_path, "acme_beta", beta_files)
    alpha_app = make_app(tmp_path, "acme_alpha")
    beta_app = make_app(tmp_path, "acme_beta")

    from fastapi import FastAPI

    host = FastAPI(title="common-app-server")
    host.mount("/alpha", alpha_app)
    host.mount("/beta", beta_app)

    alpha = call_asgi(host, "/alpha/server/health")
    beta = call_asgi(host, "/beta/server/health")

    assert alpha.status == beta.status == 200
    alpha_digest = alpha.revision_header.split(";")[0]
    beta_digest = beta.revision_header.split(";")[0]
    assert alpha_digest != beta_digest
    assert alpha_digest == f"src={source_digest_from_tree(alpha_source)}"
    assert beta_digest == f"src={source_digest_from_tree(beta_source)}"

    # Same host, same env_digest — it describes the process, not the app.
    assert alpha.revision_header.split(";")[2] == beta.revision_header.split(";")[2]

    # And each sub-app keeps its own state, as the Host router reads it.
    assert alpha_app.state.app_name == "acme-alpha"
    assert beta_app.state.app_name == "acme-beta"
    assert alpha_app.state.server_revision != beta_app.state.server_revision


# ===========================================================================
# JSON body: manifest + root
# ===========================================================================


MANIFEST = {
    "execution_mode": "automation-engine",
    "dag": {
        "extract": {
            "activity_name": "execute_workflow",
            "inputs": {"task_queue": "atlan-acme-{deployment_name}"},
        }
    },
}


def test_manifest_body_carries_version_and_revision(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    (generated / "crawler").mkdir(parents=True)
    (generated / "crawler" / "manifest.json").write_text(json.dumps(MANIFEST))
    app, source = build_stamped_app(tmp_path, "acme_manifest", generated_dir=generated)

    response = call_asgi(app, "/workflows/v1/manifest", query="entrypoint=crawler")
    assert response.status == 200
    body = response.json()

    assert body["app_version"] == "4.2.0"
    assert body["server_revision"] == {
        "app_source_digest": source_digest_from_tree(source),
        "server_sdk_declared_rev": (
            "atlan-server-sdk[aws,sql] @ git+https://github.com/atlanhq/"
            "server-sdk.git@94ece49c20aff3c8d4bded646616f4b32649aa8e"
        ),
        "env_digest": rev.environment_digest(),
    }
    # The DAG heracles reads is untouched, and the deployment token still
    # substitutes.
    assert body["execution_mode"] == "automation-engine"
    assert body["dag"]["extract"]["inputs"]["task_queue"].startswith("atlan-acme-")
    assert "{deployment_name}" not in json.dumps(body["dag"])
    # Headers are on the manifest response too.
    assert_stamped(response, source_digest_from_tree(source))


def test_manifest_stamp_never_overwrites_app_supplied_values(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "generated"
    (generated / "crawler").mkdir(parents=True)
    own = dict(MANIFEST, app_version="app-wins", server_revision="app-wins-too")
    (generated / "crawler" / "manifest.json").write_text(json.dumps(own))
    app, _ = build_stamped_app(tmp_path, "acme_mkeep", generated_dir=generated)

    body = call_asgi(app, "/workflows/v1/manifest", query="entrypoint=crawler").json()
    assert body["app_version"] == "app-wins"
    assert body["server_revision"] == "app-wins-too"


def test_manifest_stamp_survives_a_compute_hook(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    (generated / "crawler").mkdir(parents=True)
    (generated / "crawler" / "manifest.json").write_text(json.dumps(MANIFEST))

    async def reshape(base: dict, fe_inputs: dict) -> dict:
        return dict(base, reshaped=True)

    source = write_tree(tmp_path / "repo" / "acme_mhook", APP_SOURCE)
    site = tmp_path / "site-hook"
    install_into_site(
        site,
        package="acme_mhook",
        dist_name="atlan-acme-mhook",
        version="4.2.0",
        source=source,
    )
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    app = build_asgi_app(
        DefaultHandler(),
        app_name="acme-mhook",
        generated_dir=generated,
        app_package="acme_mhook",
        app_dist="atlan-acme-mhook",
        compute_manifest={"crawler": reshape},
    )

    body = call_asgi(app, "/workflows/v1/manifest", query="entrypoint=crawler").json()
    assert body["reshaped"] is True
    assert body["app_version"] == "4.2.0"
    assert body["server_revision"]["app_source_digest"] == source_digest_from_tree(
        source
    )


def test_root_route_reports_the_revision(tmp_path: Path) -> None:
    app, source = build_stamped_app(tmp_path, "acme_root")
    body = call_asgi(app, "/").json()
    assert body["app"] == "acme-root"
    assert body["app_version"] == "4.2.0"
    assert body["server_revision"]["app_source_digest"] == source_digest_from_tree(
        source
    )


# ===========================================================================
# Header safety — both halves of the stamp, not one
# ===========================================================================


def test_app_version_with_crlf_cannot_forge_a_header(tmp_path: Path) -> None:
    """``app_version`` is metadata this process did not write.

    It used to reach the wire through ``encode("ascii", "replace")`` alone,
    which is not a sanitizer: CR and LF are ASCII and pass straight through, so
    a version string containing CRLF emitted a malformed raw header and could
    append an attacker-chosen one. It now goes through the same ``header_safe``
    filter the revision half always used.
    """
    poisoned = "4.2.0\r\nX-Injected: yes\r\n"
    app = build_asgi_app(
        DefaultHandler(),
        app_name="acme-crlf",
        generated_dir=tmp_path / "no-such-generated",
        app_version=poisoned,
    )
    response = call_asgi(app, "/server/health")
    assert response.status == 200

    raw = dict(response.raw_headers)
    version_value = raw[APP_VERSION_HEADER.lower().encode()]
    assert b"\r" not in version_value and b"\n" not in version_value
    assert version_value == b"4.2.0__X-Injected:_yes__"

    # Nothing was smuggled in as a header of its own, and the stamp appears once.
    names = [name.decode().lower() for name, _ in response.raw_headers]
    assert "x-injected" not in names
    assert names.count(APP_VERSION_HEADER.lower()) == 1


def test_header_safe_folds_every_control_character() -> None:
    assert header_safe("1.0\r\nX: y") == "1.0__X:_y"
    assert header_safe("1.0\x00\x7f") == "1.0__"
    assert header_safe("") == rev.UNKNOWN
    assert header_safe(None) == rev.UNKNOWN
    # Long values are bounded, and the bound cannot reintroduce a break.
    long = header_safe("a\r\n" * 500)
    assert len(long) <= 160
    assert "\r" not in long and "\n" not in long


# ===========================================================================
# The 500 envelope is opt-in
# ===========================================================================


def test_app_without_a_package_keeps_starlettes_default_500() -> None:
    """An app that adopted nothing must not have its 500 body changed under it.

    Registering the ``Exception`` handler unconditionally turned Starlette's
    plain-text ``Internal Server Error`` into a JSON envelope for *every* app
    ``build_asgi_app`` has ever built, including ones passing no
    ``app_package``. The handler is now registered only when stamping is active.
    """
    app = build_asgi_app(DefaultHandler(), app_name="unadopted-500")

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("detonation in an unadopted app")

    response = call_asgi(app, "/boom")
    assert response.status == 500
    assert response.body == b"Internal Server Error"
    assert response.headers["content-type"].startswith("text/plain")
    # No internals leaked, and the exception still propagates for the log.
    assert b"detonation" not in response.body
    assert isinstance(response.raised, RuntimeError)


def test_adopted_app_still_gets_the_json_500(tmp_path: Path) -> None:
    """The counterpart: opting in is what buys the envelope and the stamp."""
    app, _source = build_stamped_app(tmp_path, "acme_optin500")

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("detonation")

    response = call_asgi(app, "/boom")
    assert response.status == 500
    assert response.json() == {
        "success": False,
        "data": {},
        "message": "Internal server error",
    }
    assert response.version_header == "4.2.0"


# ===========================================================================
# env_digest is computed once per process, not once per hosted app
# ===========================================================================


def test_environment_digest_is_computed_once_across_many_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consolidated host must not rescan the closure once per hosted app.

    ``server_revision``'s cache is keyed on ``(app_package, dist_name)``, so
    before ``environment_digest`` was cached, building N sub-apps ran the full
    distribution scan N times — enumerating every distribution on ``sys.path``
    and forcing a full METADATA parse each time — to produce the identical value
    every time. The closure describes the process, not the app.
    """
    names = [f"acme_env{index}" for index in range(4)]
    for name in names:
        # Distinct source per app, so the app-side half of the assertion below
        # is actually load-bearing: caching the process-wide env_digest must not
        # collapse the per-app digests along with it.
        install_app_package(
            tmp_path,
            name,
            dict(APP_SOURCE, **{"client.py": f"MARKER = {name!r}\n"}),
        )

    # Everything installed; now count what building the apps costs.
    environment_digest.cache_clear()
    real_distributions = importlib_metadata.distributions
    scans = 0

    def counting_distributions(*args: Any, **kwargs: Any) -> Any:
        nonlocal scans
        scans += 1
        return real_distributions(*args, **kwargs)

    monkeypatch.setattr(importlib_metadata, "distributions", counting_distributions)

    apps = [make_app(tmp_path, name) for name in names]

    assert len(apps) == 4
    assert scans == 1, f"the closure was scanned {scans} times for 4 apps"

    info = environment_digest.cache_info()
    assert info.misses == 1
    assert info.hits == 3
    assert info.currsize == 1

    # All four report the same env_digest — it describes the process.
    values = {app.state.server_revision.env_digest for app in apps}
    assert len(values) == 1
    assert values != {None}

    # ...while still reporting four different app_source_digests.
    assert len({app.state.server_revision.app_source_digest for app in apps}) == 4
