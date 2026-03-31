---
name: build-app
description: Build a new application on the Atlan Application SDK from scratch. Guides through discovery, design, implementation (TDD), and local testing end-to-end. Platform-agnostic — works with any AI agent or as a human-readable runbook.
---

# /build-app

Build a new application on the Atlan Application SDK — end to end.

This skill is **platform-agnostic**. It uses plain action verbs ("read the file", "create the file", "run the command") so it works with any AI coding agent (Claude Code, Codex, Gemini CLI, Cursor) or as a step-by-step runbook for human developers.

**Terminology**: "you" = the developer or agent following this skill.

---

## Hard Rules

These are non-negotiable. Violating any of them means the app will break in production.

1. **Only `App` base class.** Every app subclasses `App` directly. There are no template base classes — no `SqlMetadataExtractor`, no `SqlQueryExtractor`, no `BaseSQLMetadataExtractionWorkflow`. Even for SQL connectors, you subclass `App` and write `@task` methods with your SQL queries. Templates are a v2 pattern.

2. **Typed contracts everywhere.** Every `@task` method takes exactly one `Input` subclass parameter and returns exactly one `Output` subclass. Every `run()` method does the same. No `Dict[str, Any]`, no `*args`, no `**kwargs` at boundaries. Payload safety is validated at import time — your app won't even start if you use forbidden types (`Any`, bare `bytes`, unbounded `list`/`dict`).

3. **`run()` is deterministic.** The `run()` method is replayed by Temporal. No `datetime.now()` (use `self.now()`), no `uuid.uuid4()` (use `self.uuid()`), no I/O, no network calls, no file reads. All side effects go in `@task` methods.

4. **Infrastructure through context, not imports.** Access state, secrets, and storage via `self.context` inside `@task` methods — never import `SecretStore`, `StateStore`, or `ObjectStore` directly. Never import from `temporalio` directly. The framework handles all wiring.

5. **No placeholder code.** Every code block in this skill is real, runnable code. If something depends on the user's specific data source, the skill stops and asks for the real values. No `# TODO: implement this`, no `pass` bodies, no `"your-api-key-here"` strings.

6. **Validate relentlessly.** At every phase boundary, validate that the previous phase's output is correct. If the user's requirements seem wrong or contradictory, say so. If the SDK has a bug, flag it and tell the user to raise a PR or report it to the SDK team with the session context.

7. **Fixed project structure.** Every app follows the same layout. No creativity in directory structure.

---

## Phase 0 — Discovery & Requirements

**Goal:** Understand exactly what the user wants to build and scope it down to what's achievable.

This phase is the most important. A bad Phase 0 means a bad app. Spend time here.

### 0a — Understand the app type

Ask the user:

> What kind of application are you building? Common types:
>
> 1. **Metadata connector** — Extracts metadata (databases, schemas, tables, columns) from a data source and publishes it to Atlan's catalog
> 2. **Query log connector** — Extracts query history/usage data from a data source
> 3. **Custom automation** — A workflow that performs custom business logic (e.g., data quality checks, notifications, syncing)
> 4. **Something else** — Describe what you need

Wait for the answer. Do not proceed without it.

### 0b — Understand the data source

Based on the app type, ask targeted questions:

**For metadata/query connectors:**
- What is the data source? (e.g., Snowflake, Databricks, a REST API, a SaaS product)
- What authentication method does it use? (API key, OAuth, username/password, certificate)
- Do you have API documentation? If yes, ask the user to share the relevant sections or URLs. **Read the documentation** — do not guess the API shape.
- What entities/objects does it expose? (databases, schemas, tables, columns, dashboards, reports, etc.)
- Is there pagination? What kind? (offset, cursor, token-based)
- Are there rate limits?

**For custom automations:**
- What triggers the workflow? (scheduled, event-driven, manual)
- What external systems does it interact with?
- What is the expected input and output?

### 0c — Scope the build

After gathering requirements, write a structured scope document and present it to the user:

```
## Build Scope

### What we ARE building:
- [Concrete list of features/entities/endpoints]

### What we are NOT building (out of scope for this session):
- [Anything discussed but deferred]

### Assumptions:
- [Any assumptions about the API, auth, data shape]

### Open questions:
- [Anything still unclear]
```

**Gate: The user must confirm the scope before proceeding.** If there are open questions, resolve them first.

### 0d — Verify SDK availability

Run these commands to verify the SDK is available:

```bash
# Check if application-sdk is importable from the refactor-v3 branch
cd <project-root>
uv run python -c "from application_sdk.app import App, task; from application_sdk.contracts import Input, Output; print('SDK v3 ready')"
```

If this fails, the SDK dependency needs to be set up first (Phase 2 handles this).

---

## Phase 1 — Design Spec

**Goal:** Produce a concrete design document that maps requirements to SDK constructs.

### 1a — Map to SDK constructs

Based on the scope from Phase 0, define:

**App class:**
- Class name (PascalCase, derives to kebab-case for task queue: `SnowflakeConnector` → `snowflake-connector`)
- What `run()` orchestrates (sequence of tasks)

**Contracts (Input/Output for each boundary):**
- `run()` input: What configuration does the app need to start? (credential reference, connection config, filters)
- `run()` output: What does the app produce? (file references to extracted data, counts, status)
- Per-task input/output: What does each task need and produce?

**Rules for contracts:**
- No `Any`, no bare `bytes`, no unbounded `list`/`dict`
- Use `Annotated[list[T], MaxItems(N)]` for bounded lists — pick a realistic N
- Use `FileReference` for data larger than ~1MB (parquet files, JSON dumps)
- Use `allow_unbounded_fields=True` ONLY on the top-level `run()` Input if it receives arbitrary dicts from the Atlan platform (e.g., `connection: dict[str, Any]`)
- Every field must have a default value for backwards-compatible evolution, except truly required fields

**Tasks (`@task` methods):**
- Each task does ONE thing with side effects (network call, file write, database query)
- Name tasks by what they do: `fetch_databases`, `extract_tables`, `transform_data`, `upload_results`
- Define timeout, heartbeat, and retry settings per task based on expected duration

**Handler (if needed):**
- `test_auth`: How to validate credentials against the target system
- `preflight_check`: What checks to run before extraction (connectivity, permissions)
- `fetch_metadata`: How to list browsable objects for the UI

### 1b — Write the design document

Create a markdown document with this structure:

```markdown
# [App Name] — Design Spec

## Overview
[One paragraph: what the app does, what system it connects to, why it exists]

## App Class
- Name: `MyConnector`
- Derived queue name: `my-connector`

## Contracts

### Run Input/Output
[Table of fields with types and descriptions]

### Task: fetch_[entity]
- Input: [fields]
- Output: [fields]
- Timeout: [N seconds, with justification]
- Retry: [N attempts]
- Heartbeat: [interval, if long-running]

[Repeat for each task]

## Handler
### test_auth
[How auth is validated — what API call, what response indicates success]

### preflight_check
[What checks — connectivity, permissions, version compatibility]

### fetch_metadata
[What objects are returned for UI browsing]

## Orchestration (run method)
[Pseudocode showing the task call sequence]

## Data Flow
[How data moves: API → local files → object store → Atlan catalog]
```

### 1c — Design review

Present the design to the user and ask:

> Please review this design. Specifically:
> 1. Are the contracts correct? Any missing fields?
> 2. Is the task breakdown right? Should anything be split or merged?
> 3. Are the timeouts and retry settings realistic for your data source?
> 4. Any concerns about the handler implementation?

**Gate: User must approve the design before proceeding.**

If the design has issues that suggest an SDK limitation or bug (e.g., a contract type that should be supported but triggers `PayloadSafetyError`), flag it clearly:

> **SDK Issue Detected:** [Description of the issue]. This appears to be a limitation/bug in the SDK's `refactor-v3` branch. Please flag this to the SDK team — either raise a PR against `atlanhq/application-sdk` branch `refactor-v3`, or share this session context with the team for review.

---

## Phase 2 — Project Scaffold

**Goal:** Create the project structure with all dependencies configured.

### Fixed project structure

Every app follows this exact layout:

```
<app-name>/
├── app/
│   ├── __init__.py
│   ├── <app_name>.py          # App class with @task methods and run()
│   ├── handler.py             # Handler class (if needed)
│   └── contracts.py           # All Input/Output/HeartbeatDetails models
├── tests/
│   ├── __init__.py
│   ├── conftest.py            # Shared fixtures
│   └── unit/
│       ├── __init__.py
│       ├── test_contracts.py  # Contract validation tests
│       ├── test_handler.py    # Handler unit tests
│       └── test_app.py        # App/task unit tests
├── pyproject.toml
├── main.py                    # Local dev entry point
├── Dockerfile
├── .env.example               # Example environment variables
├── .pre-commit-config.yaml
└── .python-version            # 3.11
```

### 2a — Create pyproject.toml

```toml
[project]
name = "<app-name>"
version = "0.1.0"
description = "<one-line description from design spec>"
requires-python = ">=3.11"
dependencies = [
    "atlan-application-sdk",
]

[tool.uv.sources]
atlan-application-sdk = { git = "https://github.com/atlanhq/application-sdk", branch = "refactor-v3" }

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "ruff>=0.4",
    "pre-commit>=3.7",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "W"]
```

Add any additional dependencies the data source client requires (e.g., `httpx` for REST APIs, database drivers for SQL sources). These should have been identified in Phase 0.

### 2b — Create Dockerfile

```dockerfile
FROM registry.atlan.com/public/app-runtime-base:refactor-v3-latest

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV ATLAN_APP_MODULE=app.<app_module_name>:<AppClassName>

# Do NOT hardcode CMD ["--mode", "combined"]
# In production, Helm sets APPLICATION_MODE env var to control mode.
# The base image entrypoint.sh reads this.
CMD []
```

### 2c — Create .env.example

```bash
# Copy to .env and fill in values for local development

# Required: credentials for the target system
# (List actual credential fields from Phase 0)

# Optional: SDK configuration
LOG_LEVEL=DEBUG
ATLAN_TEMPORARY_PATH=./local/tmp/
```

### 2d — Create .python-version

```
3.11
```

### 2e — Create .pre-commit-config.yaml

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```

### 2f — Create all __init__.py files

Create empty `__init__.py` in `app/`, `tests/`, and `tests/unit/`.

### 2g — Install dependencies

```bash
cd <app-name>
uv sync --all-groups
uv run pre-commit install
```

Verify the SDK imports work:

```bash
uv run python -c "from application_sdk.app import App, task; from application_sdk.contracts import Input, Output; print('SDK ready')"
```

If this fails, stop and debug. Common issues:
- Git auth required for the SDK repo — user may need to configure SSH keys or a token
- Python version mismatch — must be 3.11+
- uv not installed — install via `curl -LsSf https://astral.sh/uv/install.sh | sh`

**Gate: Dependencies must install and SDK must import successfully before proceeding.**

---

## Phase 3 — Implementation (TDD)

**Goal:** Implement the app following test-driven development — failing test first, then minimal implementation.

Every sub-phase follows the same cycle:
1. Write a failing test
2. Run it — confirm it fails for the right reason
3. Write the minimal implementation to make it pass
4. Run it — confirm it passes
5. Refactor if needed (keep tests green)

### 3a — Contracts

**Test first.** Create `tests/unit/test_contracts.py`:

Write tests that verify:
- Each contract can be instantiated with valid data
- Each contract rejects invalid data (wrong types, missing required fields)
- Import-time payload safety validation passes (no `PayloadSafetyError`)
- Default values work correctly
- `allow_unbounded_fields=True` is ONLY used on the top-level run() Input if needed

```python
import pytest
from app.contracts import (
    # Import all contracts defined in design spec
)


class TestContracts:
    """Verify all contracts are valid and payload-safe."""

    def test_run_input_instantiation(self):
        """run() Input can be created with defaults."""
        inp = MyAppInput()
        assert inp.workflow_id == ""
        assert inp.correlation_id == ""

    def test_run_output_instantiation(self):
        """run() Output can be created with defaults."""
        out = MyAppOutput()
        # Assert default field values

    def test_task_input_instantiation(self):
        """Task Input can be created with required fields."""
        inp = FetchInput(source="test")
        assert inp.source == "test"

    def test_contracts_are_payload_safe(self):
        """All contracts pass import-time validation.

        If this test runs at all, payload safety passed.
        The SDK validates at class definition time (import time).
        A PayloadSafetyError would prevent the import above from succeeding.
        """
        # The fact that imports succeeded means validation passed.
        # This test exists as documentation.
        pass
```

Run: `uv run pytest tests/unit/test_contracts.py -v`

Expected: ImportError (contracts don't exist yet).

**Now implement.** Create `app/contracts.py` with the exact contracts from the design spec.

Rules:
- Every `Input` subclass inherits from `application_sdk.contracts.base.Input`
- Every `Output` subclass inherits from `application_sdk.contracts.base.Output`
- Use `Annotated[list[T], MaxItems(N)]` for any list field
- Use `FileReference` for large data references
- Use `SerializableEnum` for any enum that crosses task boundaries

```python
from typing import Annotated

from application_sdk.contracts.base import Input, Output, SerializableEnum
from application_sdk.contracts.types import FileReference, MaxItems


class MyAppInput(Input, allow_unbounded_fields=True):
    """Top-level run() input.

    allow_unbounded_fields=True because this receives arbitrary
    connection/metadata dicts from the Atlan platform.
    Only use this escape hatch on the top-level run() Input.
    """

    credential_guid: str = ""
    connection: dict[str, Any] = {}
    metadata: dict[str, Any] = {}


class MyAppOutput(Output):
    """Top-level run() output."""

    records_processed: int = 0
    output_ref: FileReference = FileReference()


class FetchInput(Input):
    """Input for a specific fetch task."""

    source_id: str
    batch_size: int = 1000


class FetchOutput(Output):
    """Output from a specific fetch task."""

    record_count: int = 0
    output_ref: FileReference = FileReference()
```

Run tests again: `uv run pytest tests/unit/test_contracts.py -v`

Expected: All pass.

**If tests fail:** Fix the contracts, not the tests. The tests encode the design spec.

### 3b — Handler

**Skip this sub-phase if the design spec says no handler is needed.**

**Test first.** Create `tests/unit/test_handler.py`:

```python
import pytest
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    HandlerCredential,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)

from app.handler import MyHandler


class TestHandler:
    @pytest.fixture
    def handler(self):
        return MyHandler()

    @pytest.fixture
    def valid_credentials(self):
        """Real credential structure for this data source.

        These are the credential keys the handler expects.
        Values are test values — not real secrets.
        """
        return [
            HandlerCredential(key="host", value="test-host.example.com"),
            HandlerCredential(key="api_key", value="test-api-key-12345"),
        ]

    @pytest.mark.asyncio
    async def test_test_auth_returns_auth_output(self, handler, valid_credentials):
        """test_auth returns AuthOutput with correct type."""
        inp = AuthInput(credentials=valid_credentials)
        result = await handler.test_auth(inp)
        assert isinstance(result, AuthOutput)
        assert result.status in (AuthStatus.SUCCESS, AuthStatus.FAILED)

    @pytest.mark.asyncio
    async def test_preflight_check_returns_preflight_output(self, handler, valid_credentials):
        """preflight_check returns PreflightOutput with correct type."""
        inp = PreflightInput(credentials=valid_credentials)
        result = await handler.preflight_check(inp)
        assert isinstance(result, PreflightOutput)
        assert result.status in (PreflightStatus.READY, PreflightStatus.NOT_READY)

    @pytest.mark.asyncio
    async def test_fetch_metadata_returns_metadata_output(self, handler, valid_credentials):
        """fetch_metadata returns MetadataOutput with correct type."""
        inp = MetadataInput(credentials=valid_credentials)
        result = await handler.fetch_metadata(inp)
        assert isinstance(result, MetadataOutput)
        assert isinstance(result.objects, list)
```

Run: `uv run pytest tests/unit/test_handler.py -v`

Expected: ImportError (handler doesn't exist yet).

**Now implement.** Create `app/handler.py`:

The handler must:
- Subclass `Handler` (from `application_sdk.handler.base`)
- Use typed signatures: `async def test_auth(self, input: AuthInput) -> AuthOutput`
- Never use `*args` or `**kwargs`
- Never define a `load()` method — context is injected by the framework
- Build a client from `input.credentials` (list of `HandlerCredential` key-value pairs)

```python
from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    HandlerCredential,
    MetadataInput,
    MetadataOutput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)


def _credentials_to_dict(credentials: list[HandlerCredential]) -> dict[str, str]:
    """Convert flat credential list to a dict.

    Handler endpoints receive credentials as list[HandlerCredential]
    (key-value pairs). This helper reconstructs a lookup dict.
    """
    result: dict[str, str] = {}
    for cred in credentials:
        result[cred.key] = cred.value
    return result


class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        creds = _credentials_to_dict(input.credentials)
        # Implement real auth validation against the target system.
        # Example:
        #   client = MyClient(host=creds["host"], api_key=creds["api_key"])
        #   if await client.ping():
        #       return AuthOutput(status=AuthStatus.SUCCESS)
        #   return AuthOutput(status=AuthStatus.FAILED, message="Connection refused")
        raise NotImplementedError("Implement auth validation for your data source")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        creds = _credentials_to_dict(input.credentials)
        checks: list[PreflightCheck] = []
        # Implement real preflight checks.
        # Example:
        #   checks.append(PreflightCheck(name="connectivity", passed=True))
        #   checks.append(PreflightCheck(name="permissions", passed=True))
        raise NotImplementedError("Implement preflight checks for your data source")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        creds = _credentials_to_dict(input.credentials)
        # Implement real metadata discovery.
        # Example:
        #   objects = await discover_tables(client)
        #   return MetadataOutput(objects=objects, total_count=len(objects))
        raise NotImplementedError("Implement metadata fetch for your data source")
```

**IMPORTANT:** The handler above has `raise NotImplementedError` — this is a temporary state. The agent/developer MUST replace these with real implementations using the actual API calls identified in Phase 0. Ask the user for the specific API calls if not already documented.

After implementing with real logic, run tests: `uv run pytest tests/unit/test_handler.py -v`

For handler tests that need to mock external API calls, use `unittest.mock.AsyncMock` or `pytest-httpx` depending on the client library.

### 3c — App Core

**Test first.** Create `tests/unit/test_app.py`:

```python
import pytest
from app.contracts import MyAppInput, MyAppOutput, FetchInput, FetchOutput
from app.<app_module> import MyApp


class TestAppRegistration:
    """Verify the App class is properly registered."""

    def test_app_class_exists(self):
        """App class can be imported."""
        assert MyApp is not None

    def test_app_has_run_method(self):
        """App has a run() method."""
        assert hasattr(MyApp, "run")

    def test_app_has_task_methods(self):
        """App has the expected @task methods."""
        # Check task methods exist
        assert hasattr(MyApp, "fetch_data")  # adjust to your task names


class TestAppTasks:
    """Test individual @task methods in isolation."""

    # For task tests that need infrastructure mocks:
    # - Mock self.context for state/secret/storage access
    # - Mock external API clients
    # - Use real Input/Output contracts

    @pytest.mark.asyncio
    async def test_fetch_task_returns_correct_output(self):
        """fetch task returns FetchOutput with correct fields."""
        # Setup: mock the external dependency
        # Execute: call the task method directly
        # Assert: output has expected shape and values
        pass  # Replace with real test once task is implemented
```

Run: `uv run pytest tests/unit/test_app.py -v`

Expected: ImportError (app doesn't exist yet).

**Now implement.** Create `app/<app_name>.py`:

```python
from application_sdk.app import App, task
from application_sdk.observability.logger_adaptor import get_logger

from app.contracts import (
    MyAppInput,
    MyAppOutput,
    FetchInput,
    FetchOutput,
)

logger = get_logger(__name__)


class MyApp(App):
    @task(
        timeout_seconds=3600,
        heartbeat_timeout_seconds=120,
        auto_heartbeat_seconds=30,
        retry_max_attempts=3,
    )
    async def fetch_data(self, input: FetchInput) -> FetchOutput:
        """Fetch data from the target system.

        This is a @task — it can do I/O, network calls, file writes.
        """
        logger.info("Fetching data from %s", input.source_id)

        # Implement the actual fetch logic here.
        # Access infrastructure via self.context:
        #   secret = await self.context.get_secret("my-api-key")
        #   await self.context.save_state("checkpoint", {"offset": 100})
        #   await self.context.upload_bytes("output/data.json", data)

        # For long-running tasks, heartbeat happens automatically
        # via auto_heartbeat_seconds. For manual progress:
        #   self.heartbeat(MyHeartbeatDetails(position=current_pos))

        raise NotImplementedError("Implement fetch logic")

    async def run(self, input: MyAppInput) -> MyAppOutput:
        """Orchestrate the app's tasks.

        DETERMINISTIC: No I/O, no network, no file access.
        Only call @task methods and other apps.
        Use self.now() not datetime.now().
        Use self.uuid() not uuid.uuid4().
        """
        logger.info("Starting app run")

        # Call tasks in sequence or parallel as designed
        fetch_result = await self.fetch_data(
            FetchInput(source_id="configured-source")
        )

        return MyAppOutput(
            records_processed=fetch_result.record_count,
            output_ref=fetch_result.output_ref,
        )
```

**IMPORTANT:** The `raise NotImplementedError` in tasks MUST be replaced with real implementations using the actual API/database calls from Phase 0. The agent/developer should implement these now, not defer them.

After implementing real logic, run all tests:

```bash
uv run pytest tests/ -v
```

### 3d — Entry Point

Create `main.py` for local development:

```python
"""Local development entry point.

Run with: uv run python main.py

Requires Dapr + Temporal running locally:
  uv run poe start-deps  (if using the SDK's dev tooling)
  OR start them manually (see references/local-dev.md)
"""

import asyncio

from application_sdk.main import run_dev_combined

from app.<app_module> import MyApp
from app.handler import MyHandler


if __name__ == "__main__":
    asyncio.run(
        run_dev_combined(
            MyApp,
            handler_class=MyHandler,
        )
    )
```

Create `app/__init__.py` with the app import for discoverability:

```python
from app.<app_module> import MyApp

__all__ = ["MyApp"]
```

### 3e — Verify everything compiles

```bash
# Lint and format
uv run ruff check --fix app/ tests/
uv run ruff format app/ tests/

# Run all tests
uv run pytest tests/ -v

# Verify imports work end-to-end
uv run python -c "from app.<app_module> import MyApp; print(f'App registered: {MyApp._app_name}')"
```

**Gate: All tests pass, all imports succeed, no lint errors.**

---

## Phase 4 — Local Testing

**Goal:** Run the app locally and verify handler endpoints and workflow execution.

### 4a — Start local infrastructure

The app needs Temporal (workflow engine) and optionally Dapr (state/secrets/storage) running locally.

**Option A: Using SDK dev tooling** (if working within the application-sdk repo):
```bash
uv run poe start-deps
```

**Option B: Manual setup:**
```bash
# Terminal 1: Start Temporal dev server
temporal server start-dev --ui-port 8233 --port 7233

# Terminal 2: (Optional) Start Dapr for state/secrets
# Without Dapr, the SDK falls back to InMemory + LocalStore silently.
# This is fine for initial development.
```

### 4b — Start the app

```bash
cd <app-name>
uv run python main.py
```

Wait for output like:
```
INFO: Uvicorn running on http://127.0.0.1:8000
```

### 4c — Test handler endpoints

Open a new terminal and test each endpoint:

**Auth test:**
```bash
curl -s -X POST http://localhost:8000/workflows/v1/auth \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": [
      {"key": "host", "value": "<real-test-host>"},
      {"key": "api_key", "value": "<real-test-key>"}
    ]
  }' | python -m json.tool
```

Expected response shape:
```json
{"success": true, "data": {"status": "success", "message": "..."}}
```

**Preflight check:**
```bash
curl -s -X POST http://localhost:8000/workflows/v1/check \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": [
      {"key": "host", "value": "<real-test-host>"},
      {"key": "api_key", "value": "<real-test-key>"}
    ]
  }' | python -m json.tool
```

**Metadata fetch:**
```bash
curl -s -X POST http://localhost:8000/workflows/v1/metadata \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": [
      {"key": "host", "value": "<real-test-host>"},
      {"key": "api_key", "value": "<real-test-key>"}
    ]
  }' | python -m json.tool
```

**If any endpoint returns 500:**
1. Check the app terminal for the full traceback
2. Common issues:
   - **Handler not discovered** — import the Handler class in the App module or set `ATLAN_HANDLER_MODULE`
   - **Credential format error** — ensure `_credentials_to_dict` matches what the framework sends
   - **Missing dependency** — check if the data source client library is installed

### 4d — Trigger a workflow run

```bash
curl -s -X POST http://localhost:8000/workflows/v1/start \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": [
      {"key": "host", "value": "<real-test-host>"},
      {"key": "api_key", "value": "<real-test-key>"}
    ],
    "metadata": {},
    "connection": {"connection": "dev"}
  }' | python -m json.tool
```

Capture the `workflow_id` and `run_id` from the response.

**Monitor workflow execution:**
- Open Temporal UI at http://localhost:8233
- Find your workflow by ID
- Watch the event history — each `@task` call appears as an activity

**Check workflow status:**
```bash
curl -s http://localhost:8000/workflows/v1/status/<workflow_id>/<run_id> | python -m json.tool
```

### 4e — Verify output

If the workflow writes output to the object store, check:

```bash
# Local object store path (default)
ls -la ./local/tmp/
# or
find ./local -name "*.json" -o -name "*.parquet" | head -20
```

### 4f — Fix-and-retry

If anything fails:
1. Read the error carefully — the traceback tells you exactly what went wrong
2. Fix the production code (not the tests)
3. Restart the app (Ctrl+C, then `uv run python main.py`)
4. Re-test the failing endpoint/workflow

Common issues and fixes:

| Symptom | Cause | Fix |
|---------|-------|-----|
| `PayloadSafetyError` on import | Contract uses forbidden type | Fix the contract type (see Hard Rule #2) |
| `AppContextError: context not available` | Accessing `self.context` outside `@task` | Move the access into a `@task` method |
| `NonRetryableError` in workflow | Auth/validation failure | Check credentials and input data |
| Workflow stuck/hanging | Task timeout too short | Increase `timeout_seconds` on the `@task` |
| Empty output | `output_path` not set | Compute output path in the task |

**Gate: All handler endpoints return success. Workflow completes. Output is produced.**

---

## Phase 5 — Verification & Summary

### 5a — Run full test suite

```bash
uv run pytest tests/ -v --tb=short
```

All tests must pass.

### 5b — Run linting

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
```

No errors.

### 5c — Run pre-commit

```bash
uv run pre-commit run --all-files
```

All checks pass.

### 5d — Print summary

```
## Build Summary

### App: <AppName>
- Type: <connector/automation/etc>
- Target system: <data source name>
- Base class: App

### Files Created
- app/<app_name>.py — App class with @task methods
- app/handler.py — Handler for auth/preflight/metadata
- app/contracts.py — Typed Input/Output contracts
- main.py — Local dev entry point
- Dockerfile — Production deployment
- tests/unit/test_*.py — Unit tests

### Handler Endpoints Verified
- POST /workflows/v1/auth — [PASS/FAIL]
- POST /workflows/v1/check — [PASS/FAIL]
- POST /workflows/v1/metadata — [PASS/FAIL]

### Workflow Execution
- Status: [COMPLETED/FAILED]
- Tasks executed: [list of @task methods that ran]
- Output: [description of what was produced]

### Tests
- Total: N
- Passing: N
- Failing: N

### Next Steps
1. Add integration tests with real credentials
2. Configure Helm chart for deployment
3. Set up CI/CD pipeline
4. Submit for review
```

---

## Appendix A — Quick Reference: SDK Imports

```python
# Core
from application_sdk.app import App, task
from application_sdk.contracts.base import Input, Output, HeartbeatDetails, SerializableEnum
from application_sdk.contracts.types import FileReference, MaxItems

# Handler
from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    PreflightInput, PreflightOutput, PreflightStatus, PreflightCheck,
    MetadataInput, MetadataOutput, MetadataObject, MetadataField,
    HandlerCredential,
)

# Errors
from application_sdk.app.base import NonRetryableError, AppError

# Logging
from application_sdk.observability.logger_adaptor import get_logger

# Entry point
from application_sdk.main import run_dev_combined

# Testing
from application_sdk.testing.fixtures import app_context, mock_state_store, mock_secret_store
from application_sdk.testing.mocks import MockStateStore, MockSecretStore
```

## Appendix B — Contract Rules Cheat Sheet

| Want to express | Correct type | Wrong type |
|-----------------|-------------|------------|
| A list of strings (max 100) | `Annotated[list[str], MaxItems(100)]` | `list[str]` |
| A dict of config (top-level Input only) | `dict[str, Any]` + `allow_unbounded_fields=True` | `dict[str, Any]` without flag |
| Large binary data | `FileReference` | `bytes` |
| An enum | `class Status(SerializableEnum): ...` | `class Status(Enum): ...` |
| Optional field | `field: str = ""` or `field: str \| None = None` | `field: str` (no default) |
| Anything | FORBIDDEN | `Any` |

## Appendix C — `run()` Determinism Rules

| Safe in `run()` | Unsafe in `run()` (use in `@task` only) |
|-----------------|----------------------------------------|
| `self.now()` | `datetime.now()` |
| `self.uuid()` | `uuid.uuid4()` |
| `await self.my_task(input)` | `await httpx.get(url)` |
| `await self.call(OtherApp, input)` | `open("file.txt")` |
| `self.logger.info(...)` | `await self.context.save_state(...)` |
| `if/else`, `for` loops, math | Database queries |

## Appendix D — When to Flag SDK Issues

Flag an issue to the SDK team if you encounter:

1. **Import-time crash** that isn't a contract violation — e.g., the SDK raises an unexpected error during App registration
2. **`self.context` not available** inside a `@task` method — this should always be injected by the framework
3. **Handler credentials arriving in unexpected format** — the SDK's `service.py` normalizes v2 format, but edge cases may exist
4. **Temporal serialization failures** on valid Pydantic models — should work with `pydantic_data_converter`
5. **`run_dev_combined` failing to start** — infrastructure setup errors that aren't caused by missing deps

How to flag: Either raise a PR directly against `atlanhq/application-sdk` branch `refactor-v3`, or share the error traceback and session context with the SDK team for triage.
