"""P023's reach: the blocking calls a fleet app makes that the name-only inventory missed.

The probe is one app module with one ``@task`` per blocking pattern, written the
way connector code writes it — a client bound to a local or to ``self``, a
handle from ``with open(...)``, work hidden in a sync helper. Before FND-4085
P023 caught one of these fourteen; each case is pinned separately so a
regression names the pattern it dropped.

The second half pins the one-hop rule: a sync helper is scanned exactly once,
whether it sits in the same module, on the same class, or in a repo module the
caller imports, and the shapes that keep blocking work off the loop stay silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks.determinism import scan_path, scan_text

_PROBE_HEADER = """\
import gzip
import json
import os
import shutil
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import duckdb
import httpx
from sqlalchemy import create_engine, text

from application_sdk.app import App, task


def _write_rows(path, rows):
    with open(path, "w") as fh:
        json.dump(rows, fh)


class ProbeApp(App):
    def __init__(self):
        self._ready = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=2)
"""

# (case id, @task body, the label the one finding must name) — each body holds
# exactly one blocking site, and the label pins *which* call is reported.
_PROBE_CASES: list[tuple[str, str, str]] = [
    (
        "boto3",
        's3 = boto3.client("s3")\nreturn s3.get_object(Bucket=input.bucket, Key=input.key)',
        "'s3.get_object()'",
    ),
    (
        "httpx-client",
        "with httpx.Client() as client:\n    return client.get(input.url)",
        "'client.get()'",
    ),
    ("os-listdir", "return os.listdir(input.root)", "'os.listdir()'"),
    (
        "path-iterdir",
        "return list(Path(input.root).iterdir())",
        "'Path().iterdir()'",
    ),
    (
        "open-read",
        "with open(input.path) as fh:\n    return fh.read()",
        "'fh.read()'",
    ),
    (
        "gzip",
        'with gzip.open(input.path, "wt") as fh:\n    fh.write(input.text)',
        "'fh.write()'",
    ),
    ("copyfile", "shutil.copyfile(input.src, input.dst)", "'shutil.copyfile()'"),
    (
        "zipfile",
        "with zipfile.ZipFile(input.path) as archive:\n    archive.extractall(input.root)",
        "'archive.extractall()'",
    ),
    ("fsync", "os.fsync(input.fd)", "'os.fsync()'"),
    ("event-wait", "self._ready.wait(30)", "'self._ready.wait()'"),
    (
        "future-result",
        "future = self._pool.submit(sum, input.values)\nreturn future.result()",
        "'future.result()'",
    ),
    (
        "sqlalchemy",
        "engine = create_engine(input.url)\n"
        "with engine.connect() as conn:\n"
        '    return conn.execute(text("select 1"))',
        "'conn.execute()'",
    ),
    (
        "duckdb",
        "con = duckdb.connect()\nreturn con.execute(input.query).fetchall()",
        "'con.execute()'",
    ),
    ("sync-helper", "_write_rows(input.path, input.rows)", "'_write_rows()'"),
]


def _probe_task(name: str, body: str) -> str:
    method = name.replace("-", "_")
    lines = "\n".join("        " + line for line in body.splitlines())
    return f"\n    @task\n    async def probe_{method}(self, input):\n{lines}\n"


def _probe_module() -> tuple[str, dict[str, range]]:
    """The probe source, and the line range each case's task occupies."""
    source = _PROBE_HEADER
    spans: dict[str, range] = {}
    for name, body, _ in _PROBE_CASES:
        start = source.count("\n") + 1
        source += _probe_task(name, body)
        spans[name] = range(start, source.count("\n") + 1)
    return source, spans


def _p023(source: str, file: str = "app/probe.py") -> list:
    return [f for f in scan_text(source, file) if f.rule_id == "P023"]


@pytest.mark.parametrize(
    ("case", "label"), [(c, label) for c, _, label in _PROBE_CASES]
)
def test_probe_case_is_caught_exactly_once(case: str, label: str) -> None:
    source, spans = _probe_module()
    hits = [f for f in _p023(source) if f.line in spans[case]]
    assert len(hits) == 1, (case, [f.message for f in hits])
    assert label in hits[0].message, (case, hits[0].message)


def test_probe_reports_nothing_outside_the_cases() -> None:
    source, spans = _probe_module()
    covered = {line for span in spans.values() for line in span}
    assert [f for f in _p023(source) if f.line not in covered] == []


# ── one hop into sync helpers ────────────────────────────────────────────────


_TASK_HEADER = "import shutil\nfrom application_sdk.app import App, task\n"


def test_helper_finding_names_the_call_it_reaches() -> None:
    src = (
        _TASK_HEADER + "def _purge(path):\n"
        "    shutil.rmtree(path)\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        _purge(input.path)\n"
    )
    (finding,) = _p023(src)
    assert finding.line == 8
    assert "'_purge()'" in finding.message
    assert "shutil.rmtree()" in finding.message
    assert "app/probe.py:4" in finding.message


def test_helper_reached_through_a_sync_self_method() -> None:
    src = (
        _TASK_HEADER + "class A(App):\n"
        "    def _purge(self, path):\n"
        "        shutil.rmtree(path)\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        self._purge(input.path)\n"
    )
    (finding,) = _p023(src)
    assert finding.line == 8


def test_helper_sees_its_own_self_clients() -> None:
    src = (
        "import requests\nfrom application_sdk.app import App, task\n"
        "class A(App):\n"
        "    def __init__(self):\n"
        "        self._http = requests.Session()\n"
        "    def _fetch(self, url):\n"
        "        return self._http.get(url)\n"
        "    @task\n"
        "    async def fetch(self, input):\n"
        "        return self._fetch(input.url)\n"
    )
    (finding,) = _p023(src)
    assert finding.line == 10


def test_offloaded_helper_is_silent() -> None:
    src = (
        _TASK_HEADER + "from application_sdk.execution.heartbeat import run_in_thread\n"
        "def _purge(path):\n"
        "    shutil.rmtree(path)\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        await run_in_thread(_purge, input.path)\n"
        "        await run_in_thread(lambda: _purge(input.path))\n"
    )
    assert _p023(src) == []


def test_helper_without_blocking_work_is_silent() -> None:
    src = (
        _TASK_HEADER + "def _name(path):\n"
        "    return str(path).upper()\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        return _name(input.path)\n"
    )
    assert _p023(src) == []


def test_async_helper_is_not_a_hop() -> None:
    src = (
        _TASK_HEADER + "async def _purge(path):\n"
        "    shutil.rmtree(path)\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        await _purge(input.path)\n"
    )
    # The rmtree is reported once, inside the async helper itself.
    (finding,) = _p023(src)
    assert finding.line == 4


def test_only_one_hop_is_followed() -> None:
    src = (
        _TASK_HEADER + "def _inner(path):\n"
        "    shutil.rmtree(path)\n"
        "def _outer(path):\n"
        "    _inner(path)\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        _outer(input.path)\n"
    )
    assert _p023(src) == []


def test_recursive_helper_terminates() -> None:
    src = (
        _TASK_HEADER + "def _walk(path):\n"
        "    return _walk(path)\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        _walk(input.path)\n"
    )
    assert _p023(src) == []


def test_suppression_inside_the_helper_is_honoured() -> None:
    src = (
        _TASK_HEADER + "def _purge(path):\n"
        "    # conformance: ignore[P023] tiny fixed-size scratch dir\n"
        "    shutil.rmtree(path)\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        _purge(input.path)\n"
    )
    assert _p023(src) == []


def test_local_name_shadowing_a_helper_is_not_followed() -> None:
    src = (
        _TASK_HEADER + "def _purge(path):\n"
        "    shutil.rmtree(path)\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input, _purge):\n"
        "        _purge(input.path)\n"
    )
    assert _p023(src) == []


def test_helper_call_in_workflow_context_is_left_to_p021() -> None:
    src = (
        "import shutil\nfrom application_sdk.app import App, task, entrypoint\n"
        "def _purge(path):\n"
        "    shutil.rmtree(path)\n"
        "class A(App):\n"
        "    @entrypoint\n"
        "    async def run(self, input):\n"
        "        _purge(input.path)\n"
    )
    assert _p023(src) == []


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


_IMPORTED_HELPER = "import shutil\ndef purge(path):\n    shutil.rmtree(path)\n"


@pytest.mark.parametrize(
    "import_line",
    ["from app.util import purge", "from .util import purge"],
)
def test_helper_imported_from_a_repo_module(tmp_path: Path, import_line: str) -> None:
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/util.py", _IMPORTED_HELPER)
    caller = _write(
        tmp_path,
        "app/main.py",
        f"{import_line}\nfrom application_sdk.app import App, task\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        purge(input.path)\n",
    )
    (finding,) = [f for f in scan_path(caller, tmp_path) if f.rule_id == "P023"]
    assert finding.line == 6
    assert "app/util.py:3" in finding.message


def test_helper_from_a_third_party_module_is_not_followed(tmp_path: Path) -> None:
    caller = _write(
        tmp_path,
        "app/main.py",
        "from somelib.util import purge\nfrom application_sdk.app import App, task\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        purge(input.path)\n",
    )
    assert [f for f in scan_path(caller, tmp_path) if f.rule_id == "P023"] == []


# ── deprecated modules ───────────────────────────────────────────────────────


_DEPRECATED_BODY = (
    "import shutil\nfrom application_sdk.app import App, task\n"
    "class A(App):\n"
    "    @task\n"
    "    async def clean(self, input):\n"
    "        shutil.rmtree(input.path)\n"
)


def test_deprecated_module_is_skipped() -> None:
    src = (
        "import warnings\n"
        'warnings.warn("gone in v4", DeprecationWarning, stacklevel=2)\n'
        + _DEPRECATED_BODY
    )
    assert _p023(src) == []


def test_module_under_a_deprecated_package_is_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app/legacy/__init__.py",
        'import warnings\nwarnings.warn("gone", DeprecationWarning)\n',
    )
    caller = _write(tmp_path, "app/legacy/io.py", _DEPRECATED_BODY)
    assert [f for f in scan_path(caller, tmp_path) if f.rule_id == "P023"] == []


def test_class_deprecated_at_construction_is_skipped() -> None:
    src = (
        "import shutil\nimport warnings\nfrom application_sdk.app import App, task\n"
        "class OldWriter:\n"
        "    def __init__(self):\n"
        '        warnings.warn("gone in v4", DeprecationWarning, stacklevel=2)\n'
        "    async def close(self, path):\n"
        "        shutil.rmtree(path)\n"
        "class Live(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        shutil.rmtree(input.path)\n"
    )
    (finding,) = _p023(src)
    assert finding.line == 12


def test_uncalled_nested_warning_does_not_deprecate_the_class() -> None:
    """A warn inside a nested def of `__init__` never runs at construction, so
    it says nothing about the class; its live methods are still scanned."""
    src = (
        "import shutil\nimport warnings\nfrom application_sdk.app import App, task\n"
        "class Live(App):\n"
        "    def __init__(self):\n"
        "        def _legacy():\n"
        '            warnings.warn("old path", DeprecationWarning)\n'
        "        self._legacy = _legacy\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        shutil.rmtree(input.path)\n"
    )
    (finding,) = _p023(src)
    assert finding.line == 11


def test_helper_in_a_deprecated_module_is_not_followed(tmp_path: Path) -> None:
    _write(tmp_path, "app/__init__.py", "")
    _write(
        tmp_path,
        "app/legacy/__init__.py",
        'import warnings\nwarnings.warn("gone", DeprecationWarning)\n',
    )
    _write(tmp_path, "app/legacy/util.py", _IMPORTED_HELPER)
    caller = _write(
        tmp_path,
        "app/main.py",
        "from app.legacy.util import purge\nfrom application_sdk.app import App, task\n"
        "class A(App):\n"
        "    @task\n"
        "    async def clean(self, input):\n"
        "        purge(input.path)\n",
    )
    assert [f for f in scan_path(caller, tmp_path) if f.rule_id == "P023"] == []


def test_live_module_is_still_flagged(tmp_path: Path) -> None:
    caller = _write(tmp_path, "app/live.py", _DEPRECATED_BODY)
    assert len([f for f in scan_path(caller, tmp_path) if f.rule_id == "P023"]) == 1


# ── new callables: the silent side ──────────────────────────────────────────


@pytest.mark.parametrize(
    "stmt",
    [
        # Constructors and lookups that do no I/O.
        'boto3.client("s3").generate_presigned_url("get_object")',
        'boto3.resource("s3").Bucket("b")',
        "httpx.Client()",
        "open(input.path)",
        "threading.Event().set()",
        "create_engine(input.url)",
        # Awaited: an async object wearing a sync name.
        "await input.client.get(input.url)",
        # The handle comes from an async context manager, not builtin open().
        "async with aiofiles.open(input.path) as fh:\n            await fh.read()",
    ],
)
def test_new_inventory_constructors_and_async_forms_are_silent(stmt: str) -> None:
    src = (
        "import threading\nimport aiofiles\nimport boto3\nimport httpx\n"
        "from sqlalchemy import create_engine\n"
        "from application_sdk.app import App, task\n"
        "class A(App):\n"
        "    @task\n"
        "    async def t(self, input):\n"
        f"        {stmt}\n"
    )
    assert _p023(src) == [], stmt


def test_sqlalchemy_connect_alone_is_not_reported() -> None:
    """The connect pairs with the execute that follows it; reporting both would
    double-count one block, so a connect with no execute is silent."""
    src = (
        "from sqlalchemy import create_engine\n"
        "from application_sdk.app import App, task\n"
        "class A(App):\n"
        "    @task\n"
        "    async def t(self, input):\n"
        "        engine = create_engine(input.url)\n"
        "        with engine.connect() as conn:\n"
        "            pass\n"
    )
    assert _p023(src) == []


@pytest.mark.parametrize(
    "stmt",
    [
        "await httpx.Client().get(input.url)",
        "with open(input.path) as fh:\n            await fh.read()",
    ],
)
def test_awaiting_a_known_sync_client_is_still_flagged(stmt: str) -> None:
    """The request runs before `await` raises on its non-awaitable result, so an
    `await` on a client already identified as sync is a bug, not an async API."""
    src = (
        "import httpx\nfrom application_sdk.app import App, task\n"
        "class A(App):\n"
        "    @task\n"
        "    async def t(self, input):\n"
        f"        {stmt}\n"
    )
    assert len(_p023(src)) == 1, stmt


@pytest.mark.parametrize(
    "stmt",
    [
        'with input.path.open("rb") as fh:\n            fh.read(1024)',
        "with Path(input.path).open() as fh:\n            fh.read()",
        "fh = input.path.open()\n        fh.read()",
    ],
)
def test_path_open_handle_is_tracked(stmt: str) -> None:
    src = (
        "from pathlib import Path\nfrom application_sdk.app import App, task\n"
        "class A(App):\n"
        "    @task\n"
        "    async def t(self, input):\n"
        f"        {stmt}\n"
    )
    assert len(_p023(src)) == 1, stmt


def test_open_on_a_tracked_client_is_not_a_file_handle() -> None:
    """`o.open(url)` on a urllib opener is the send itself, reported once; its
    result is a response, not a handle whose `read()` is reported again."""
    src = (
        "import urllib.request\nfrom application_sdk.app import App, task\n"
        "class A(App):\n"
        "    @task\n"
        "    async def t(self, input):\n"
        "        o = urllib.request.build_opener()\n"
        "        resp = o.open(input.url)\n"
        "        resp.read()\n"
    )
    assert [f.line for f in _p023(src)] == [7]


def test_rebinding_a_file_handle_in_async_with_clears_it() -> None:
    src = (
        "import aiofiles\nfrom application_sdk.app import App, task\n"
        "class A(App):\n"
        "    @task\n"
        "    async def t(self, input):\n"
        "        with open(input.a) as fh:\n"
        "            pass\n"
        "        async with aiofiles.open(input.b) as fh:\n"
        "            data = fh.read()\n"
    )
    assert _p023(src) == []


# ── workflow context: P021 owns it, and must actually see it ─────────────────


@pytest.mark.parametrize(
    "stmt",
    [
        'boto3.client("s3").get_object(Bucket="b", Key="k")',
        's3 = boto3.client("s3")',
        "con = duckdb.connect()",
        "engine = create_engine(input.url)",
        "z = zipfile.ZipFile(input.path)",
        "fh = gzip.open(input.path)",
    ],
)
def test_workflow_client_work_is_p021_not_p023(stmt: str) -> None:
    src = (
        "import gzip\nimport zipfile\nimport boto3\nimport duckdb\n"
        "from sqlalchemy import create_engine\n"
        "from application_sdk.app import App, entrypoint\n"
        "class A(App):\n"
        "    @entrypoint\n"
        "    async def run(self, input):\n"
        f"        {stmt}\n"
    )
    findings = scan_text(src, "app/wf.py")
    assert [f.rule_id for f in findings if f.rule_id in ("P021", "P023")] == [
        "P021"
    ], stmt


@pytest.mark.parametrize(
    "stmt",
    [
        'q = sqlalchemy.text("SELECT 1")',
        "cfg = botocore.config.Config(connect_timeout=5)",
        "t = duckdb.typing.VARCHAR",
    ],
)
def test_workflow_in_memory_builders_are_not_io(stmt: str) -> None:
    src = (
        "import sqlalchemy\nimport botocore.config\nimport duckdb\n"
        "from application_sdk.app import App, entrypoint\n"
        "class A(App):\n"
        "    @entrypoint\n"
        "    async def run(self, input):\n"
        f"        {stmt}\n"
    )
    findings = scan_text(src, "app/wf.py")
    assert [f for f in findings if f.rule_id in ("P021", "P023")] == [], stmt
