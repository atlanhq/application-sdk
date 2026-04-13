# Proposal: Refactor `application_sdk/common/`

## Problem

`common/` has become a dumping ground. It holds 10 top-level files and 16 incremental extraction files covering at least 15 distinct concerns — from exception wrapping to AWS credentials to DuckDB storage. This makes it hard to find things and creates surprising dependency chains.

## Guiding Principle

A file belongs in `common/` only if it has **zero domain knowledge** and is consumed by **3+ unrelated modules**. Everything else should live next to the code it serves.

---

## Proposed Changes

### 1. Delete `file_converter.py` — unused in production

- **Consumers:** 0 production, 1 test
- **Action:** Delete the file and its test
- **Risk:** None — no production code imports it

### 2. Move `types.py` → `io/types.py`

- **Consumers:** 6, all in `io/` or `io/` tests
- **Contains:** `DataframeType` enum (pandas | daft)
- **Why:** Every consumer is I/O related. This is an I/O concern, not a common one.

### 3. Move `file_ops.py` + `path.py` → `storage/file_ops.py`

- **Consumers:** `io/json.py`, `io/parquet.py`, `services/objectstore.py`, `services/statestore.py`
- **Contains:** `SafeFileOps` (atomic file operations) and `convert_to_extended_path()` (Windows path fix)
- **Why:** File operations are a storage concern. `path.py` has no external consumers — it's only used by `file_ops.py`. Merge them.

### 4. Move `aws_utils.py` → `clients/aws_utils.py`

- **Consumers:** 2 — `clients/sql.py` and its test
- **Contains:** AWS RDS/Redshift token generation, boto3 wrappers
- **Why:** Only the SQL client uses it. It's a client concern.

### 5. Move `sql_utils.py` → `clients/sql_utils.py`

- **Consumers:** 0 external (only tests)
- **Contains:** SQL query preparation, filter normalization, POSIX regex transformation
- **Why:** SQL-specific utilities belong next to the SQL client. Currently depends on `utils.py` for filter/query helpers — those move here too.

### 6. Break up `utils.py` (600+ LOC, 13 concerns)

This is the critical change. `utils.py` is a grab bag that should be split:

| Functions | Move to | Why |
|-----------|---------|-----|
| `prepare_query()`, `prepare_filters()`, `normalize_filters()`, `transform_posix_regex()`, `extract_database_names_from_regex_common()`, `get_database_names()` | `clients/sql_utils.py` | SQL/filter specific, only used by SQL clients |
| `parse_credentials_extra()`, `parse_filter_input()` | `credentials/utils.py` | Credential parsing belongs with credentials |
| `get_actual_cpu_count()`, `get_safe_num_threads()` | `common/concurrency.py` | Truly generic, keep in common |
| `has_custom_control_config()` | `contracts/` or inline | Config check, single-purpose |
| `get_file_names()`, `read_sql_files()` | `io/utils.py` (already exists) or `storage/` | File listing/reading |
| `download_file_from_upload_response()` | `storage/` | Object store operation |
| `run_sync()` | `common/concurrency.py` | Generic async utility |

After the split, delete `utils.py`.

### 7. Merge `error_codes.py` into top-level `errors.py`

- **Consumers:** 16
- **Problem:** Two parallel error systems exist — `common/error_codes.py` (enum-based: `CommonError`, `ActivityError`) and `errors.py` (structured: `ErrorCode`, `AppError`)
- **Action:** Migrate the enum definitions from `error_codes.py` into `errors.py`. Update the 16 import sites.
- **Note:** This is high-consumer-count so it touches many files. Can be done as a separate PR.

### 8. Promote `incremental/` → `application_sdk/incremental/`

- **Size:** 16 files, self-contained feature subsystem
- **External consumer:** 1 — `templates/incremental_sql_metadata_extractor.py`
- **Internal deps:** Only `common/exc_utils.py`, `constants`, `observability`, `services/objectstore`
- **Why:** It's bigger than most top-level modules. It has its own models, state machine, storage backends, README. It's a feature, not a utility.
- **Also:** Move `incremental/skills/` → `.claude/skills/` (skill definitions don't belong in the Python package)

### 9. Move `native-orchestration/skills/` → `.claude/skills/`

- **Contains:** Claude Code skill definitions (`.md` files only)
- **Why:** No Python code. Skill definitions live in `.claude/skills/`, not inside the SDK package.

---

## What stays in `common/`

After all moves, `common/` contains only:

| File | Consumers | Why it stays |
|------|-----------|-------------|
| `exc_utils.py` | 13 | Zero-dependency exception wrapper. Used by clients, execution, io, services, templates. Intentionally has no SDK imports to avoid circular deps. |
| `models.py` | 2 | Shared `TaskStatistics` / `TaskResult` models. Generic task metadata. |
| `concurrency.py` (new) | ~3 | `get_actual_cpu_count()`, `get_safe_num_threads()`, `run_sync()` — generic threading/async utilities. |

That's it. Three files. A proper `common/` module.

---

## Migration Order

Changes are ordered to minimize breakage. Each step is independently committable.

| Phase | Change | Files touched | Risk |
|-------|--------|--------------|------|
| 1 | Delete `file_converter.py` | 2 | None |
| 2 | Move `types.py` → `io/types.py` | 7 | Low |
| 3 | Move `file_ops.py` + `path.py` → `storage/file_ops.py` | 7 | Low |
| 4 | Move `aws_utils.py` → `clients/aws_utils.py` | 3 | None |
| 5 | Break up `utils.py` + move `sql_utils.py` → `clients/sql_utils.py` | ~15 | Medium |
| 6 | Move skills to `.claude/skills/` | 5 | None |
| 7 | Promote `incremental/` → `application_sdk/incremental/` | ~20 | Medium |
| 8 | Merge `error_codes.py` → `errors.py` | ~18 | Medium |

Total: ~8 PRs or 3 batched PRs.

---

## What this does NOT change

- `exc_utils.py` stays in `common/` — it's the canonical example of a common utility
- `models.py` stays — generic task metadata models
- No v3 API changes — all moves are internal, import paths change but public API doesn't
- `services/objectstore.py` and other deferred deprecations are untouched
- Migration tooling (`tools/migrate_v3/`) is untouched

---

## Verification

After each phase:
1. `uv run pre-commit run --all-files`
2. `uv run pytest tests/unit/ -x -q`
3. Grep for old import paths to catch stragglers
