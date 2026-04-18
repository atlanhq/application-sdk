"""Runner for CI E2E example tests.

Starts each example app as a subprocess, triggers a workflow via HTTP,
polls for completion, and writes results to workflow_status.md.

Dapr and Temporal must already be running (via `uv run poe start-deps`)
before invoking this script.

Usage (from examples/):
    python run_examples.py
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Repo root is one level above examples/
_REPO_ROOT = Path(__file__).parent.parent

PORT = 8000
BASE_URL = f"http://127.0.0.1:{PORT}"
HEALTH_TIMEOUT = 60
WORKFLOW_TIMEOUT = 300
POLL_INTERVAL = 5


@dataclass
class ExampleConfig:
    name: str
    module: str
    workflow_input: dict[str, Any]
    skip_if_missing_env: list[str] = field(default_factory=list)
    # Set to True for skeleton/stub examples that should never be run in CI.
    is_stub: bool = False


def _build_examples() -> list[ExampleConfig]:
    postgres_host = os.environ.get("POSTGRES_HOST", "")
    postgres_password = os.environ.get("POSTGRES_PASSWORD", "")
    snowflake_account = os.environ.get("SNOWFLAKE_ACCOUNT_ID", "")
    snowflake_user = os.environ.get("SNOWFLAKE_USER", "")
    snowflake_password = os.environ.get("SNOWFLAKE_PASSWORD", "")

    return [
        ExampleConfig(
            name="application_hello_world",
            module="application_hello_world",
            workflow_input={"name": "World"},
        ),
        ExampleConfig(
            name="application_fastapi",
            module="application_fastapi",
            workflow_input={"name": "test"},
        ),
        ExampleConfig(
            name="application_sql",
            module="application_sql",
            workflow_input={
                "connection": {
                    "connection_name": "test-connection",
                    "connection_qualified_name": "default/postgres/1728518400",
                },
                "credentials": [
                    {"key": "host", "value": postgres_host},
                    {"key": "username", "value": "postgres"},
                    {"key": "password", "value": postgres_password},
                    {"key": "port", "value": "5432"},
                    {"key": "database", "value": "postgres"},
                ],
            },
            skip_if_missing_env=["POSTGRES_HOST", "POSTGRES_PASSWORD"],
        ),
        ExampleConfig(
            name="application_sql_with_custom_transformer",
            module="application_sql_with_custom_transformer",
            workflow_input={
                "connection": {
                    "connection_name": "test-connection",
                    "connection_qualified_name": "default/postgres/1728518400",
                },
                "credentials": [
                    {"key": "host", "value": postgres_host},
                    {"key": "username", "value": "postgres"},
                    {"key": "password", "value": postgres_password},
                    {"key": "port", "value": "5432"},
                    {"key": "database", "value": "postgres"},
                ],
            },
            skip_if_missing_env=["POSTGRES_HOST", "POSTGRES_PASSWORD"],
            is_stub=True,
        ),
        ExampleConfig(
            name="application_sql_miner",
            module="application_sql_miner",
            workflow_input={
                "connection": {
                    "connection_name": "test-connection",
                    "connection_qualified_name": "default/snowflake/1728518400",
                },
                "credentials": [
                    {"key": "account_id", "value": snowflake_account},
                    {"key": "username", "value": snowflake_user},
                    {"key": "password", "value": snowflake_password},
                ],
                "lookback_days": 7,
                "batch_size": 1000,
            },
            is_stub=True,
        ),
    ]


def _http_get(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"_error": str(exc)}


def _http_post(url: str, data: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {"_error": f"HTTP {exc.code}: {exc.read().decode()[:200]}"}
    except Exception as exc:
        return {"_error": str(exc)}


def _wait_for_health(timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _http_get(f"{BASE_URL}/health")
        if result.get("status") == "healthy":
            return True
        time.sleep(2)
    return False


def _start_workflow(workflow_input: dict[str, Any]) -> tuple[str, str] | None:
    response = _http_post(f"{BASE_URL}/workflows/v1/start", workflow_input)
    data = response.get("data", {})
    workflow_id = data.get("workflow_id")
    run_id = data.get("run_id")
    if workflow_id and run_id:
        return workflow_id, run_id
    print(f"    start response: {response}", flush=True)
    return None


def _poll_for_completion(workflow_id: str, run_id: str, timeout: int) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _http_get(f"{BASE_URL}/workflows/v1/status/{workflow_id}/{run_id}")
        status = result.get("data", {}).get("status", "")
        if status and status != "RUNNING":
            return status
        time.sleep(POLL_INTERVAL)
    return "TIMEOUT"


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            time.sleep(2)
        proc.kill()
        proc.wait(timeout=15)
    except Exception:
        pass


def run_example(example: ExampleConfig) -> tuple[str, float]:
    if example.is_stub:
        print("  SKIP — skeleton example (not a runnable implementation)", flush=True)
        return "SKIPPED", 0.0

    missing = [k for k in example.skip_if_missing_env if not os.environ.get(k)]
    if missing:
        print(f"  SKIP — missing env: {', '.join(missing)}", flush=True)
        return "SKIPPED", 0.0

    env = os.environ.copy()
    env["ATLAN_LOCAL_DEVELOPMENT"] = "true"
    # Components live in the repo root, not in examples/
    env.setdefault("DAPR_COMPONENTS_PATH", str(_REPO_ROOT / "components"))

    extra_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        extra_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    # Import the module under its real name (not __main__) so Temporal's
    # sandbox can resolve workflow classes via the module path rather than
    # falling back to the unresolvable __temporal_main__.
    launcher = (
        "import sys, importlib.util, asyncio\n"
        "sys.path.insert(0, '.')\n"
        f"spec = importlib.util.spec_from_file_location({example.module!r}, {example.module!r} + '.py')\n"
        f"mod = importlib.util.module_from_spec(spec)\n"
        f"sys.modules[{example.module!r}] = mod\n"
        "spec.loader.exec_module(mod)\n"
        "from application_sdk.main import run_dev_combined\n"
        "from application_sdk.app import App as _App\n"
        "_app_cls = next(\n"
        "    (v for v in vars(mod).values()\n"
        "     if isinstance(v, type) and issubclass(v, _App) and v is not _App\n"
        f"     and v.__module__ == {example.module!r}),\n"
        "    None,\n"
        ")\n"
        "if _app_cls is None:\n"
        f"    raise RuntimeError('No App subclass found in {example.module}')\n"
        "asyncio.run(run_dev_combined(_app_cls))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", launcher],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **extra_kwargs,
    )

    start = time.monotonic()
    try:
        print("  waiting for server...", flush=True)
        if not _wait_for_health(HEALTH_TIMEOUT):
            return "SERVER_TIMEOUT", time.monotonic() - start

        print("  server ready, starting workflow...", flush=True)
        ids = _start_workflow(example.workflow_input)
        if ids is None:
            return "START_FAILED", time.monotonic() - start

        workflow_id, run_id = ids
        print(f"  workflow {workflow_id}, polling...", flush=True)
        status = _poll_for_completion(workflow_id, run_id, WORKFLOW_TIMEOUT)
        elapsed = time.monotonic() - start
        print(f"  {status} ({elapsed:.1f}s)", flush=True)
        return status, elapsed
    finally:
        _kill(proc)


def main() -> None:
    examples = _build_examples()

    with open("workflow_status.md", "w", encoding="utf-8") as f:
        f.write("## 📦 Example workflows test results\n")
        f.write("- This workflow runs all the examples in the `examples` directory.\n")
        f.write("-----------------------------------\n")
        f.write("| Example | Status | Time Taken |\n")
        f.write("| --- | --- | --- |\n")

    failed: list[str] = []

    for example in examples:
        print(f"\nRunning {example.name}...", flush=True)
        status, elapsed = run_example(example)
        time_fmt = f"{elapsed:.2f} seconds" if elapsed > 0 else "N/A"

        with open("workflow_status.md", "a", encoding="utf-8") as f:
            f.write(f"| {example.name} | {status} | {time_fmt} |\n")

        if status not in ("COMPLETED", "SKIPPED"):
            failed.append(f"{example.name} ({status})")

    with open("workflow_status.md", "a", encoding="utf-8") as f:
        f.write(
            "> This is an automatically generated file. Please do not edit directly.\n"
        )

    if failed:
        print(f"\nFailed: {', '.join(failed)}", flush=True)
        sys.exit(1)

    print("\nAll examples completed.", flush=True)


if __name__ == "__main__":
    main()
