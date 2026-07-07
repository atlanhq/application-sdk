"""Meta-tests for the P-series determinism / async-correctness checks (P020–P024, P031).

Each rule is tested to fire *exactly* when it should and stay silent otherwise —
both false positives and false negatives are guarded.  The load-bearing properties
get dedicated tests: ``@task`` activity bodies are never flagged, the sanctioned
SDK seam (``self.now()`` / ``self.uuid()`` / ``now`` / ``sleep``) is left untouched
(receiver-anchored matching), decorator classification is alias-aware, and the
P020/P023 dedup holds (a workflow-context blocking call is P020, not also P023).
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


def test_p031_exempts_heartbeat_module() -> None:
    from conformance.suite.checks.determinism import scan_text as _scan_text

    body = "import asyncio\nasync def f():\n    return await asyncio.to_thread(g)\n"
    findings = [
        f
        for f in _scan_text(body, "application_sdk/execution/heartbeat.py")
        if f.rule_id == "P031"
    ]
    assert findings == []


def test_p031_suppression() -> None:
    body = (
        "import asyncio\n"
        "async def f():\n"
        "    return await asyncio.to_thread(g)  # conformance: ignore[P031] legacy\n"
    )
    findings = _rule(body, "P031", header="")
    assert len(findings) == 1 and findings[0].suppressed


# ── catalog meta-tests ───────────────────────────────────────────────────────


def test_new_rules_present_and_scoped_both() -> None:
    for rid in ("P020", "P021", "P022", "P023", "P024", "P031"):
        assert rid in CATALOG, f"{rid} missing from catalog"
        assert CATALOG[rid].scope is RuleScope.BOTH
        assert CATALOG[rid].rationale.strip(), f"{rid} needs a non-empty rationale"
