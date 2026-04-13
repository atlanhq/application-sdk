# v3 Documentation vs Code Gap Report

Audit date: 2026-04-10  |  Branch: `refactor-v3` (`cfe3fb4`)

Seven gaps where the docs and code disagree. Each gap has a recommended fix
direction: update the **code** to match the documented API, or update the
**docs** to match the implementation.

---

## Overview

| # | Gap | Breaks at | Recommendation | Scope |
|---|-----|-----------|----------------|-------|
| 1 | `run_dev_combined(handler_class=...)` | `TypeError` | Fix code | 6 doc files |
| 2 | `run_dev_combined(secret_store=..., state_store=...)` | `TypeError` | Fix code | 6 doc files |
| 3 | `self.context.download_bytes()` / `upload_bytes()` | `AttributeError` | Fix docs | 1 doc file |
| 4 | `on_complete(self, success: bool)` | `TypeError` on override | Fix code | 4 doc files |
| 5 | `await self.cleanup_files()` (no args) | `TypeError` | Fix docs | 3 doc files |
| 6 | `passthrough_modules` as class keyword | Silent no-op | Fix code | 3 doc files |
| 7 | `MockHeartbeatController.recorded_heartbeats` | `AttributeError` | Fix docs | 2 doc files |

---

## Fix in code (gaps 1, 2, 4, 6)

These gaps describe a better API than what the code currently provides. The
docs represent the intended design; the code should catch up.

### Gap 1 — `run_dev_combined` should accept `handler_class`

Every example in the docs passes `handler_class` directly:

```python
asyncio.run(run_dev_combined(MyExtractor, handler_class=MyHandler))
```

The actual signature (`main.py:961`) has no such parameter. The handler is
loaded from the `ATLAN_HANDLER_MODULE` env var, which is hostile to the
"single-call local dev" ergonomics `run_dev_combined` is meant to provide.

**Suggested code change** — add `handler_class: type | None = None` to the
signature. When provided, derive the module string
(`f"{cls.__module__}:{cls.__name__}"`) and pass it as `handler_module` to
`AppConfig` instead of reading the env var.

**Docs using this pattern:**

- `docs/migration-guide-v3.md` — lines 260, 821
- `docs/concepts/entry-points.md` — lines 72, 88
- `docs/concepts/server.md` — line 73
- `docs/whats-new-v3.md` — lines 344, 425, 459

---

### Gap 2 — `run_dev_combined` should accept `secret_store` and `state_store`

Docs show the intuitive API:

```python
asyncio.run(run_dev_combined(
    MyConnector,
    secret_store=InMemorySecretStore({"my-api-key": "test-value"}),
    state_store=InMemoryStateStore(),
))
```

The actual signature only accepts `credential_stores: Mapping[str, SecretStore]`,
a lower-level abstraction that requires wrapping in a dict. There is no
`state_store` parameter at all — the state store is always `InMemoryStateStore`
when Dapr is absent.

**Suggested code change** — add `secret_store` and `state_store` keyword
arguments alongside `credential_stores`. When `secret_store` is provided,
use it directly instead of pulling from the mapping. When `state_store` is
provided, pass it to `_create_infrastructure`. Keep `credential_stores` for
backward compatibility.

**Docs using this pattern:**

- `docs/migration-guide-v3.md` — lines 460-463, 819-824
- `docs/concepts/entry-points.md` — lines 84-91
- `docs/whats-new-v3.md` — lines 343-346
- `docs/concepts/tasks.md` — lines 175-176
- `docs/concepts/handlers.md` — line 189
- `docs/concepts/apps.md` — lines 217-218

> **Note:** The `InfrastructureContext(secret_store=..., state_store=...)`
> pattern used in the *testing* sections (e.g., migration-guide-v3.md:763) is
> correct — that is the dataclass constructor, not `run_dev_combined`.

---

### Gap 4 — `on_complete` should receive `success: bool`

Docs consistently show:

```python
async def on_complete(self, success: bool) -> None:
    if success:
        await self.notify_downstream()
```

The actual signature is `on_complete(self) -> None` (`base.py:1212`). The
caller at `base.py:1427` calls `await self.on_complete()` with no arguments
— it has access to whether `run()` succeeded but does not pass that info.

Without the `success` flag, users who override `on_complete` have no way to
distinguish success from failure, which defeats the purpose of a lifecycle
hook.

**Suggested code change** — add `success: bool` parameter to `on_complete`,
and pass it from the caller (the `finally` block at `base.py:1423-1431`
already knows the outcome).

**Docs using this pattern:**

- `docs/migration-guide-v3.md` — lines 705, 716
- `docs/whats-new-v3.md` — lines 628, 636
- `docs/concepts/apps.md` — line 163
- `docs/guides/sql-application-guide.md` — line 732

---

### Gap 6 — `passthrough_modules` should work as a class keyword

Docs show the Pythonic class-keyword syntax:

```python
class MyConnector(App, passthrough_modules=["my_connector", "third_party_lib"]):
    ...
```

The code declares `passthrough_modules` as a `ClassVar[set[str] | None]`
(`base.py:422`). The `__init_subclass__(**kwargs)` method does not extract
`passthrough_modules` from kwargs — it reads `cls.passthrough_modules` at
line 503, which only sees the ClassVar default (`None`) unless the user
redeclares it in the class body.

The class keyword is silently swallowed by `object.__init_subclass__`,
so the modules are never registered. This is a silent bug — no error, no
sandboxing.

**Suggested code change** — extract `passthrough_modules` from `**kwargs` in
`__init_subclass__` and merge with the ClassVar default. Accept both
`list` and `set` types; coerce to `set` internally. This makes both
syntaxes work:

```python
# Class keyword (docs style)
class MyConnector(App, passthrough_modules=["my_connector"]):
    ...

# ClassVar (current code-only style)
class MyConnector(App):
    passthrough_modules = {"my_connector"}
```

**Docs using this pattern:**

- `docs/migration-guide-v3.md` — line 313
- `docs/whats-new-v3.md` — line 162
- `docs/concepts/apps.md` — line 184

---

## Fix in docs (gaps 3, 5, 7)

These gaps reflect intentional code design where the docs got the details wrong.

### Gap 3 — `self.context` does not have `download_bytes` / `upload_bytes`

The migration table in Step 8 claims:

| v2 | v3 |
|----|-----|
| `ObjectStore.get_content(key)` | `await self.context.download_bytes(key)` |
| `ObjectStore.upload_file(src, key)` | `await self.context.upload_bytes(key, data)` |

Neither method exists on `AppContext` (`app/context.py:189-386`). The v3
storage design is deliberately file-based and streaming — there is no
bytes-in-memory convenience API on the context.

`AppContext` exposes a `storage` property (line 319) that returns the
underlying `ObjectStore`, and users call module-level functions:

```python
from application_sdk.storage import upload_file, download_file

await upload_file("output/file.parquet", "/tmp/file.parquet", store=self.context.storage)
await download_file("config/settings.json", "/tmp/settings.json", store=self.context.storage)
```

**Doc fix** — replace the two rows in the migration table
(`migration-guide-v3.md:406-407`) with the `upload_file` / `download_file`
pattern above. Add a note that `self.context.storage` provides the store
handle.

---

### Gap 5 — `cleanup_files()` / `cleanup_storage()` require typed input

Docs show a zero-arg call:

```python
async def on_complete(self, success: bool) -> None:
    await self.cleanup_files()
    await self.cleanup_storage()
```

Both are `@task` methods that require typed inputs:

- `cleanup_files(self, input: CleanupInput)` — `base.py:1011`
- `cleanup_storage(self, input: StorageCleanupInput)` — `base.py:1090`

The default `on_complete()` already calls both with empty inputs
(`CleanupInput()` and `StorageCleanupInput()` at lines 1234, 1240). Users
who want the default cleanup should just call `await super().on_complete()`
and not invoke these directly.

**Doc fix** — replace the manual `cleanup_files()` / `cleanup_storage()`
examples with:

```python
async def on_complete(self) -> None:
    await self.notify_downstream()
    await super().on_complete()   # handles cleanup automatically
```

If showing explicit calls, include the required input:
`await self.cleanup_files(CleanupInput())`.

**Affected docs:**

- `docs/migration-guide-v3.md` — lines 718-721
- `docs/concepts/apps.md` — lines 166-167
- `docs/whats-new-v3.md` — lines 639-640

---

### Gap 7 — `MockHeartbeatController` API is `get_heartbeat_calls()`, not `recorded_heartbeats`

Docs say:

```python
# controller.recorded_heartbeats contains all calls made
```

Actual API (`testing/mocks.py:205`):

```python
calls = controller.get_heartbeat_calls()  # -> list[tuple[Any, ...]]
```

**Doc fix** — s/`recorded_heartbeats`/`get_heartbeat_calls()`/ in:

- `docs/migration-guide-v3.md` — line 809
- `docs/whats-new-v3.md` — line 720

---

## Dependency graph

Gaps 1, 2, and 4 are related: they all touch `run_dev_combined` and the
`on_complete` lifecycle. If the code fixes are done together:

1. Add `handler_class`, `secret_store`, `state_store` to `run_dev_combined`
2. Add `success: bool` to `on_complete` and the call site in `_run_workflow`
3. Extract `passthrough_modules` from `__init_subclass__` kwargs

Then the only doc fixes needed are gaps 3, 5, and 7 (all small, localized
text edits).
