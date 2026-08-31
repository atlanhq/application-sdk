"""Meta-tests for the P-series determinism / async-correctness checks (P020–P024, P031).

Each rule is tested to fire *exactly* when it should and stay silent otherwise —
both false positives and false negatives are guarded.  The load-bearing properties
get dedicated tests: ``@task`` activity bodies are never flagged, the sanctioned
SDK seam (``self.now()`` / ``self.uuid()`` / ``now`` / ``sleep``) is left untouched
(receiver-anchored matching), decorator classification is alias-aware, and the
P020/P023 dedup holds (a workflow-context blocking call is P020, not also P023).

P023's tree-scale filesystem coverage is guarded on both sides: ``shutil.rmtree``
/ ``copytree`` / ``move`` (and the ``SafeFileOps`` wrappers) fire inside an
``async def``, the offloaded ``run_in_thread(shutil.rmtree, path)`` form does not,
single-syscall ops (``os.remove`` / ``unlink`` / ``rmdir``) are deliberately
silent, and workflow-context tree ops belong to P021 rather than being
double-reported here.
"""

from __future__ import annotations

from conformance.suite.checks.determinism import SERIES, scan_text
from conformance.suite.rules import CATALOG
from conformance.suite.schema.disposition import RuleScope

_HEADER = (
    "from application_sdk.app import App, task, entrypoint, signal, query, update\n"
)


def _rule(body: str, rule_id: str, *, header: str = _HEADER) -> list:
    """Findings of *rule_id* from scanning a module with *body* appended to *header*."""
    return [f for f in scan_text(header + body, "app/x.py") if f.rule_id == rule_id]


def _wrap_run(stmts: str) -> str:
    """An ``App`` subclass whose async ``run`` body is *stmts* (4-space indented)."""
    indented = "\n".join("        " + line for line in stmts.strip("\n").splitlines())
    return f"class MyApp(App):\n    async def run(self, input):\n{indented}\n"


# ── series wiring ─────────────────────────────────────────────────────────────


def test_series_letter() -> None:
    assert SERIES == "P"


# ── P020 NonDeterministicPrimitiveInWorkflow ─────────────────────────────────


def test_p020_flags_datetime_now() -> None:
    src = "import datetime\n" + _wrap_run("x = datetime.datetime.now()")
    assert len(_rule(src, "P020")) == 1


def test_p020_flags_time_time() -> None:
    src = "import time\n" + _wrap_run("x = time.time()")
    assert len(_rule(src, "P020")) == 1


def test_p020_flags_uuid4() -> None:
    src = "import uuid\n" + _wrap_run("x = uuid.uuid4()")
    assert len(_rule(src, "P020")) == 1


def test_p020_flags_time_sleep_and_asyncio_sleep() -> None:
    src = "import time, asyncio\n" + _wrap_run("time.sleep(1)\nawait asyncio.sleep(2)")
    assert len(_rule(src, "P020")) == 2


def test_p020_flags_random() -> None:
    src = "import random\n" + _wrap_run("x = random.randint(0, 9)")
    assert len(_rule(src, "P020")) == 1


def test_p020_flags_in_entrypoint_and_interaction_handlers() -> None:
    body = (
        "import datetime\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def go(self, input):\n"
        "        return datetime.datetime.now()\n"
        "    @signal\n"
        "    async def ping(self):\n"
        "        self.t = datetime.datetime.utcnow()\n"
    )
    assert len(_rule(body, "P020")) == 2


def test_p020_alias_aware_interaction_decorator() -> None:
    body = (
        "import datetime\n"
        "from application_sdk.app import App, query as q\n"
        "class MyApp(App):\n"
        "    @q\n"
        "    def status(self):\n"
        "        return datetime.datetime.now()\n"
    )
    assert len(_rule(body, "P020", header="")) == 1


def test_p020_silent_on_sanctioned_self_helpers() -> None:
    src = _wrap_run("a = self.now()\nb = self.uuid()")
    assert _rule(src, "P020") == []


def test_p020_silent_on_sanctioned_seam_imports() -> None:
    body = (
        "from application_sdk.app import App, now, uuid4, sleep\n"
        "class MyApp(App):\n"
        "    async def run(self, input):\n"
        "        a = now()\n"
        "        b = uuid4()\n"
        "        await sleep(1)\n"
    )
    assert _rule(body, "P020", header="") == []


def test_p020_silent_in_task_activity() -> None:
    body = (
        "import datetime, time\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fetch(self, input):\n"
        "        time.sleep(1)\n"
        "        return datetime.datetime.now()\n"
    )
    assert _rule(body, "P020") == []


def test_p020_silent_outside_app_subclass() -> None:
    body = "import datetime\nclass Plain:\n    async def run(self, input):\n        return datetime.datetime.now()\n"
    assert _rule(body, "P020") == []


def test_p020_uuid5_is_deterministic_and_silent() -> None:
    src = "import uuid\n" + _wrap_run("x = uuid.uuid5(uuid.NAMESPACE_DNS, 'a')")
    assert _rule(src, "P020") == []


def test_p020_suppression() -> None:
    src = "import datetime\n" + _wrap_run(
        "x = datetime.datetime.now()  # conformance: ignore[P020] tested elsewhere"
    )
    findings = _rule(src, "P020")
    assert len(findings) == 1 and findings[0].suppressed


# ── P021 SideEffectIoInWorkflow ──────────────────────────────────────────────


def test_p021_flags_requests() -> None:
    src = "import requests\n" + _wrap_run("r = requests.get('http://x')")
    assert len(_rule(src, "P021")) == 1


def test_p021_flags_open_builtin() -> None:
    src = _wrap_run("f = open('/tmp/x')")
    assert len(_rule(src, "P021")) == 1


def test_p021_flags_os_environ_subscript() -> None:
    src = "import os\n" + _wrap_run("v = os.environ['HOME']")
    assert len(_rule(src, "P021")) == 1


def test_p021_flags_thread_spawn() -> None:
    src = "import threading\n" + _wrap_run("threading.Thread(target=None).start()")
    assert len(_rule(src, "P021")) >= 1


def test_p021_flags_shutil_filesystem_mutation() -> None:
    """Every shutil entry point touches the filesystem — it belongs in a @task.

    This is also what keeps P023's workflow-context skip a dedup rather than a
    hole: without it, a tree op in workflow context was reported by nobody.
    """
    src = "import shutil\n" + _wrap_run(
        "shutil.rmtree('/tmp/out')\nshutil.copytree('/a', '/b')"
    )
    assert len(_rule(src, "P021")) == 2


def test_p021_silent_in_task_activity() -> None:
    body = (
        "import requests\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fetch(self, input):\n"
        "        return requests.get('http://x')\n"
    )
    assert _rule(body, "P021") == []


# ── P022 UnawaitedCoroutine ──────────────────────────────────────────────────


def test_p022_flags_bare_self_task_call() -> None:
    body = (
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fetch(self, input):\n"
        "        return input\n"
        "    async def run(self, input):\n"
        "        self.fetch(input)\n"
    )
    assert len(_rule(body, "P022")) == 1


def test_p022_silent_when_awaited() -> None:
    body = (
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fetch(self, input):\n"
        "        return input\n"
        "    async def run(self, input):\n"
        "        return await self.fetch(input)\n"
    )
    assert _rule(body, "P022") == []


def test_p022_silent_when_wrapped_in_create_task() -> None:
    body = (
        "import asyncio\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fetch(self, input):\n"
        "        return input\n"
        "    async def run(self, input):\n"
        "        asyncio.create_task(self.fetch(input))\n"
    )
    # The create_task call itself isn't a dropped same-class coroutine.
    assert _rule(body, "P022") == []


# ── P023 BlockingCallInAsyncDef ──────────────────────────────────────────────


def test_p023_flags_asyncio_run() -> None:
    body = (
        "import asyncio\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fetch(self, input):\n"
        "        return asyncio.run(other())\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_flags_run_until_complete() -> None:
    body = (
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fetch(self, input):\n"
        "        loop = get_loop()\n"
        "        return loop.run_until_complete(other())\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_flags_blocking_io_in_async_activity() -> None:
    body = (
        "import requests\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fetch(self, input):\n"
        "        return requests.get('http://x')\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_silent_in_sync_def() -> None:
    body = (
        "import requests\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    def fetch(self, input):\n"
        "        return requests.get('http://x')\n"
    )
    assert _rule(body, "P023") == []


def test_p023_dedup_workflow_sleep_is_p020_not_p023() -> None:
    src = "import time\n" + _wrap_run("time.sleep(1)")
    assert len(_rule(src, "P020")) == 1
    assert _rule(src, "P023") == []


def test_p023_flags_rmtree_in_async_activity() -> None:
    """The App.cleanup_files bug class: a tree-scale rmtree on the event loop."""
    body = (
        "import shutil\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def cleanup(self, input):\n"
        "        shutil.rmtree('/tmp/out')\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_flags_rmtree_via_from_import() -> None:
    """Receiver-anchored resolution: ``from shutil import rmtree`` still matches."""
    body = (
        "from shutil import rmtree\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def cleanup(self, input):\n"
        "        rmtree('/tmp/out')\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_flags_copytree_and_move() -> None:
    body = (
        "import shutil\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def stage(self, input):\n"
        "        shutil.copytree('/a', '/b')\n"
        "        shutil.move('/b', '/c')\n"
    )
    assert len(_rule(body, "P023")) == 2


def test_p023_flags_safefileops_wrapper() -> None:
    """Routing through the SDK's own wrapper is not a way around the rule."""
    body = (
        "from application_sdk.common.file_ops import SafeFileOps\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def cleanup(self, input):\n"
        "        SafeFileOps.rmtree('/tmp/out', ignore_errors=True)\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_flags_safefileops_move_wrapper() -> None:
    """The move wrapper is matched too (SafeFileOps wraps rmtree and move)."""
    body = (
        "from application_sdk.common.file_ops import SafeFileOps\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def stage(self, input):\n"
        "        SafeFileOps.move('/a', '/b')\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_silent_for_safefileops_lookalike_class() -> None:
    """The wrapper match is anchored on the segment boundary: a class whose name
    merely *ends in* ``SafeFileOps`` (``MySafeFileOps`` / ``NotSafeFileOps``) is
    not the SDK wrapper and must not be flagged."""
    body = (
        "class MyApp(App):\n"
        "    @task\n"
        "    async def cleanup(self, input):\n"
        "        MySafeFileOps.rmtree('/tmp/out')\n"
        "        NotSafeFileOps.move('/a', '/b')\n"
    )
    assert _rule(body, "P023") == []


def test_p023_silent_when_rmtree_is_offloaded() -> None:
    """``run_in_thread(shutil.rmtree, path)`` passes the callable, never calls it."""
    body = (
        "import shutil\n"
        "from application_sdk.execution.heartbeat import run_in_thread\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def cleanup(self, input):\n"
        "        await run_in_thread(shutil.rmtree, '/tmp/out')\n"
    )
    assert _rule(body, "P023") == []


def test_p023_silent_for_single_syscall_fs_ops() -> None:
    """One inode operation does not earn a thread hop — deliberately not flagged."""
    body = (
        "import os\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def cleanup(self, input):\n"
        "        os.remove('/tmp/f')\n"
        "        os.unlink('/tmp/g')\n"
        "        os.rmdir('/tmp/d')\n"
    )
    assert _rule(body, "P023") == []


def test_p023_silent_for_rmtree_in_sync_def() -> None:
    body = "import shutil\n" "def cleanup(path):\n" "    shutil.rmtree(path)\n"
    assert _rule(body, "P023") == []


def test_p023_dedup_workflow_rmtree_is_p021_not_p023() -> None:
    """File I/O in workflow context is P021's; P023 must not double-report."""
    src = "import shutil\n" + _wrap_run("shutil.rmtree('/tmp/out')")
    assert _rule(src, "P023") == []


# ── P023 data-scale inventory ────────────────────────────────────────────────
#
# Every pattern below was found blocking a loop in a real connector during the
# fleet sweep that motivated the extension; the app is named in each docstring
# so a future reader can check the finding is still worth having.


def test_p023_flags_pandas_read_sql() -> None:
    """A connector fallback path ran pd.read_sql per database on the loop."""
    body = (
        "import pandas as pd\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def dbs(self, input):\n"
        "        return pd.read_sql(query, engine)\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_flags_pandas_read_parquet_and_frame_writer() -> None:
    """A connector read parquet on the loop; the writer half is a method call."""
    body = (
        "import pandas as pd\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def out(self, input):\n"
        "        df = pd.read_parquet(path)\n"
        "        df.to_parquet(other)\n"
    )
    assert len(_rule(body, "P023")) == 2


def test_p023_flags_pyarrow_parquet_read_table() -> None:
    """A connector called pq.read_table straight from an enrichment activity."""
    body = (
        "from pyarrow import parquet as pq\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def enrich(self, input):\n"
        "        return pq.read_table(path)\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_flags_tree_traversal() -> None:
    """The sweep the module docstring deferred. All four forms occur in the
    fleet; Path.glob / Path.rglob are the most common finding of any pattern."""
    body = (
        "import glob\n"
        "import os\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def scan(self, input):\n"
        "        a = os.walk(root)\n"
        "        b = glob.glob(pattern)\n"
        "        c = root.glob('*.json')\n"
        "        d = root.rglob('*')\n"
    )
    assert len(_rule(body, "P023")) == 4


def test_p023_traversal_reports_glob_glob_once() -> None:
    """`glob.glob` matches the exact set AND the `.glob` suffix — exact wins,
    and the early return keeps it a single finding."""
    body = (
        "import glob\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def scan(self, input):\n"
        "        return glob.glob(pattern)\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_flags_whole_file_pathlib_accessors() -> None:
    """A connector rewrote each JSON file in place with read_text/write_text."""
    body = (
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fix(self, input):\n"
        "        text = path.read_text()\n"
        "        path.write_text(text)\n"
    )
    assert len(_rule(body, "P023")) == 2


def test_p023_flags_file_handle_serialization() -> None:
    """json.dump / json.load against an open handle, per output file."""
    body = (
        "import json\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def write(self, input):\n"
        "        json.dump(payload, handle)\n"
        "        return json.load(handle)\n"
    )
    assert len(_rule(body, "P023")) == 2


def test_p023_silent_for_in_memory_json_string_forms() -> None:
    """`loads`/`dumps` are CPU on a small payload, not file-scale I/O."""
    body = (
        "import json\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def write(self, input):\n"
        "        blob = json.dumps(payload)\n"
        "        return json.loads(blob)\n"
    )
    assert _rule(body, "P023") == []


def test_p023_silent_for_serializers_with_no_load_loads_split() -> None:
    """Only (de)serializers whose *name* guarantees a file object are matched.

    PyYAML and `csv` have no `load`/`loads` split — `yaml.safe_load` and
    `csv.reader` take a string or a plain iterable as readily as a handle — so
    matching them by name would flag exactly the in-memory parsing that
    `json.loads` is deliberately allowed to do.  Neither appeared in the fleet
    sweep either.  This pins the exclusion so the names cannot drift back into
    the exact set without a file-handle heuristic to justify them.
    """
    body = (
        "import csv\n"
        "import yaml\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def parse(self, input):\n"
        "        cfg = yaml.safe_load(blob)\n"
        "        out = yaml.dump(cfg)\n"
        "        rows = list(csv.reader(lines))\n"
        "        return cfg, out, rows\n"
    )
    assert _rule(body, "P023") == []


def test_p023_flags_serializers_that_require_a_handle() -> None:
    """The counterpart: `pickle.load` and `tomllib.load` have no string form
    that shares their name, so a match is always file work."""
    body = (
        "import pickle\n"
        "import tomllib\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def read(self, input):\n"
        "        a = pickle.load(handle)\n"
        "        b = tomllib.load(handle)\n"
        "        return a, b\n"
    )
    assert len(_rule(body, "P023")) == 2


def test_p023_flags_subprocess() -> None:
    body = (
        "import subprocess\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def build(self, input):\n"
        "        subprocess.run(['ls'])\n"
    )
    assert len(_rule(body, "P023")) == 1


def test_p023_silent_for_awaited_lookalikes() -> None:
    """An awaited `read_text`/`glob`/`to_sql` is an async API (anyio.Path, an
    async ORM) wearing a blocking name. The name cannot tell them apart; the
    `await` can, so an awaited call is never flagged."""
    body = (
        "class MyApp(App):\n"
        "    @task\n"
        "    async def read(self, input):\n"
        "        text = await path.read_text()\n"
        "        rows = await path.glob('*')\n"
        "        return text, rows\n"
    )
    assert _rule(body, "P023") == []


def test_p023_silent_when_data_io_is_offloaded() -> None:
    """`run_in_thread(pd.read_parquet, path)` passes the callable, never calls it."""
    body = (
        "import pandas as pd\n"
        "from application_sdk.execution.heartbeat import run_in_thread\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def read(self, input):\n"
        "        return await run_in_thread(pd.read_parquet, path)\n"
    )
    assert _rule(body, "P023") == []


def test_p023_data_io_silent_in_sync_def() -> None:
    """The offload closure itself is a sync def — flagging it would flag the fix."""
    body = (
        "import pandas as pd\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def read(self, input):\n"
        "        def _read():\n"
        "            return pd.read_parquet(path)\n"
        "        return await self.task_context.run_in_thread(_read)\n"
    )
    assert _rule(body, "P023") == []


def test_p023_dedup_workflow_data_io_is_p021_not_p023() -> None:
    """Data-scale I/O in workflow context stays P021's, like the FS work."""
    src = "import pandas as pd\n" + _wrap_run("pd.read_parquet('/tmp/f')")
    assert _rule(src, "P023") == []


# ── P024 SyncAtlanClientInApp ────────────────────────────────────────────────


def test_p024_flags_sync_atlanclient_construction() -> None:
    body = (
        "from pyatlan.client.atlan import AtlanClient\n"
        "def make():\n"
        "    return AtlanClient(base_url='x', api_key='y')\n"
    )
    assert len(_rule(body, "P024", header="")) == 1


def test_p024_flags_from_token_factory() -> None:
    body = "from pyatlan.client.atlan import AtlanClient\nc = AtlanClient.from_token('t')\n"
    assert len(_rule(body, "P024", header="")) == 1


def test_p024_flags_attribute_construction() -> None:
    body = "import pyatlan\nc = pyatlan.client.atlan.AtlanClient(base_url='x')\n"
    assert len(_rule(body, "P024", header="")) == 1


def test_p024_alias_aware() -> None:
    body = (
        "from pyatlan.client.atlan import AtlanClient as Client\n"
        "c = Client(base_url='x')\n"
    )
    assert len(_rule(body, "P024", header="")) == 1


def test_p024_silent_on_async_client() -> None:
    body = "from pyatlan.client.aio import AsyncAtlanClient\nc = AsyncAtlanClient(base_url='x')\n"
    assert _rule(body, "P024", header="") == []


def test_p024_silent_on_sdk_seam() -> None:
    body = (
        "from application_sdk.credentials import create_async_atlan_client\n"
        "c = create_async_atlan_client(cred)\n"
    )
    assert _rule(body, "P024", header="") == []


def test_p024_silent_on_non_pyatlan_same_name() -> None:
    # A same-named class from an unrelated package must not be flagged.
    body = "from mypkg import AtlanClient\nc = AtlanClient()\n"
    assert _rule(body, "P024", header="") == []


def test_p024_suppression() -> None:
    body = (
        "from pyatlan.client.atlan import AtlanClient\n"
        "c = AtlanClient()  # conformance: ignore[P024] legacy sync path\n"
    )
    findings = _rule(body, "P024", header="")
    assert len(findings) == 1 and findings[0].suppressed


# ── P031 SharedDefaultExecutorOffload ────────────────────────────────────────


def test_p031_flags_asyncio_to_thread() -> None:
    body = "import asyncio\nasync def f():\n    return await asyncio.to_thread(g)\n"
    assert len(_rule(body, "P031", header="")) == 1


def test_p031_flags_to_thread_from_import() -> None:
    body = (
        "from asyncio import to_thread\n"
        "async def f():\n"
        "    return await to_thread(g)\n"
    )
    assert len(_rule(body, "P031", header="")) == 1


def test_p031_flags_run_in_executor_none() -> None:
    body = "async def f(loop):\n" "    return await loop.run_in_executor(None, g)\n"
    assert len(_rule(body, "P031", header="")) == 1


def test_p031_flags_get_event_loop_run_in_executor_none() -> None:
    body = (
        "import asyncio\n"
        "async def f():\n"
        "    return await asyncio.get_event_loop().run_in_executor(None, g)\n"
    )
    assert len(_rule(body, "P031", header="")) == 1


def test_p031_silent_on_custom_executor() -> None:
    body = (
        "async def f(loop, pool):\n" "    return await loop.run_in_executor(pool, g)\n"
    )
    assert _rule(body, "P031", header="") == []


def test_p031_exempts_the_offload_module() -> None:
    from conformance.suite.checks.determinism import scan_text as _scan_text

    body = "import asyncio\nasync def f():\n    return await asyncio.to_thread(g)\n"
    findings = [
        f
        for f in _scan_text(body, "application_sdk/_runtime/offload.py")
        if f.rule_id == "P031"
    ]
    assert findings == []


def test_p031_does_not_exempt_the_app_facing_reexport() -> None:
    """The exemption follows the implementation, not the façade (ADR-0019).

    ``execution/heartbeat.py`` re-exports ``run_in_thread`` but constructs no
    executor of its own, so exempting it would hand a free pass to a module that
    has no reason to need one.
    """
    from conformance.suite.checks.determinism import scan_text as _scan_text

    body = "import asyncio\nasync def f():\n    return await asyncio.to_thread(g)\n"
    findings = [
        f
        for f in _scan_text(body, "application_sdk/execution/heartbeat.py")
        if f.rule_id == "P031"
    ]
    assert len(findings) == 1


def test_p031_suppression() -> None:
    body = (
        "import asyncio\n"
        "async def f():\n"
        "    return await asyncio.to_thread(g)  # conformance: ignore[P031] legacy\n"
    )
    findings = _rule(body, "P031", header="")
    assert len(findings) == 1 and findings[0].suppressed


# ── P036 HandRolledProcessIsolation ───────────────────────────────────────────


def test_p036_flags_process_pool_executor_dotted() -> None:
    body = (
        "import concurrent.futures\n"
        "def f():\n"
        "    return concurrent.futures.ProcessPoolExecutor(max_workers=1)\n"
    )
    assert len(_rule(body, "P036", header="")) == 1


def test_p036_flags_process_pool_executor_aliased_import() -> None:
    body = (
        "from concurrent.futures import ProcessPoolExecutor as PPE\n"
        "def f():\n"
        "    return PPE()\n"
    )
    assert len(_rule(body, "P036", header="")) == 1


def test_p036_flags_multiprocessing_process() -> None:
    body = (
        "import multiprocessing\n"
        "def f(target):\n"
        "    return multiprocessing.Process(target=target)\n"
    )
    assert len(_rule(body, "P036", header="")) == 1


def test_p036_flags_multiprocessing_pool() -> None:
    body = "import multiprocessing\ndef f():\n    return multiprocessing.Pool(2)\n"
    assert len(_rule(body, "P036", header="")) == 1


def test_p036_silent_on_thread_pool_executor() -> None:
    # ThreadPoolExecutor is a thread pool, not a process — out of scope (P031
    # governs shared-default-executor thread offload).
    body = (
        "from concurrent.futures import ThreadPoolExecutor\n"
        "def f():\n"
        "    return ThreadPoolExecutor()\n"
    )
    assert _rule(body, "P036", header="") == []


def test_p036_exempts_the_offload_module() -> None:
    from conformance.suite.checks.determinism import scan_text as _scan_text

    body = (
        "import concurrent.futures\n"
        "def f():\n"
        "    return concurrent.futures.ProcessPoolExecutor()\n"
    )
    findings = [
        f
        for f in _scan_text(body, "application_sdk/_runtime/offload.py")
        if f.rule_id == "P036"
    ]
    assert findings == []


def test_p036_does_not_exempt_the_app_facing_reexport() -> None:
    """The exemption follows the implementation, not the façade (ADR-0019).

    ``execution/heartbeat.py`` re-exports ``run_fault_isolated`` but constructs no
    process pool of its own, so it must be held to the rule like any other module.
    """
    from conformance.suite.checks.determinism import scan_text as _scan_text

    body = (
        "import concurrent.futures\n"
        "def f():\n"
        "    return concurrent.futures.ProcessPoolExecutor()\n"
    )
    findings = [
        f
        for f in _scan_text(body, "application_sdk/execution/heartbeat.py")
        if f.rule_id == "P036"
    ]
    assert len(findings) == 1


def test_p036_suppression() -> None:
    body = (
        "from concurrent.futures import ProcessPoolExecutor\n"
        "def f():\n"
        "    return ProcessPoolExecutor()  # conformance: ignore[P036] cpu-bound, off-worker\n"
    )
    findings = _rule(body, "P036", header="")
    assert len(findings) == 1 and findings[0].suppressed


# ── catalog meta-tests ───────────────────────────────────────────────────────


def test_new_rules_present_and_scoped_both() -> None:
    for rid in ("P020", "P021", "P022", "P023", "P024", "P031", "P036"):
        assert rid in CATALOG, f"{rid} missing from catalog"
        assert CATALOG[rid].scope is RuleScope.BOTH
        assert CATALOG[rid].rationale.strip(), f"{rid} needs a non-empty rationale"
