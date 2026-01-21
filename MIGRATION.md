# Migration Guide: Dependency Groups Reorganization (v0.3.0)

This guide helps you transition from the old dependency group names to the new user-friendly structure.

## Breaking Changes

As of v0.3.0, we've reorganized our dependency groups to be more intuitive and workflow-focused. The old group names will continue to work until **v0.4.0** but are deprecated.

## Dependency Group Mapping

| Old Name | New Name | Changes |
|----------|----------|---------|
| `workflows` | `workflows_sql` or `workflows_http` | Now includes `daft` by default |
| `iam_auth` | `auth_aws` | Renamed for consistency |
| `azure` | `auth_azure` | Renamed for consistency |
| `daft` | *(included in workflows)* | No longer separate |
| `sqlalchemy` | *(included in workflows_sql)* | Bundled with SQL workflows |
| `scale_data_generator` | `scale_data_generator` | Unchanged |
| `distributed_lock` | `distributed_lock` | Unchanged |
| `test` (group) | `tests` (group) | Consolidated naming |

## Migration Steps

### 1. Update Installation Commands

**Before:**
```bash
pip install atlan-application-sdk[workflows,daft,iam_auth]
uv add atlan-application-sdk --extra workflows --extra daft --extra iam_auth
```

**After:**
```bash
pip install atlan-application-sdk[workflows_sql,auth_aws]
uv add atlan-application-sdk --extra workflows_sql --extra auth_aws
```

### 2. Update pyproject.toml / requirements.txt

**Before:**
```toml
dependencies = [
    "atlan-application-sdk[workflows,iam_auth,daft]",
]
```

**After:**
```toml
dependencies = [
    "atlan-application-sdk[workflows_sql,auth_aws]",
]
```

### 3. Update uv sync Commands

**Before:**
```bash
uv sync --extra workflows --extra daft --group test
```

**After:**
```bash
uv sync --extra workflows_sql --group tests
```

## New Dependency Groups

### Workflow Groups
| Group | Description | Includes |
|-------|-------------|----------|
| `workflows_sql` | SQL-based data extraction workflows | dapr, temporalio, orjson, daft, sqlalchemy |
| `workflows_http` | HTTP/REST-based workflows | dapr, temporalio, orjson, daft |

### Authentication Groups
| Group | Description | Includes |
|-------|-------------|----------|
| `auth_aws` | AWS authentication | boto3 |
| `auth_azure` | Azure authentication & storage | azure-identity, azure-storage-blob, azure-storage-file-datalake |
| `auth_gcp` | Google Cloud authentication | *(coming soon)* |

### Other Groups
| Group | Description |
|-------|-------------|
| `test_utils` | Testing utilities (pytest-order, pandera, pandas) |
| `pandas` | Pandas data processing |
| `scale_data_generator` | Large-scale test data generation |
| `distributed_lock` | Redis-based distributed locking |
| `mcp` | Model Context Protocol support |

### Development Groups (via `--group`)
| Group | Description |
|-------|-------------|
| `dev` | Development tools (pre-commit, mkdocs, etc.) |
| `tests` | Testing framework (pytest, coverage, hypothesis) |
| `examples` | Example application dependencies |

## Installation Examples

```bash
# SQL workflows with AWS auth
pip install atlan-application-sdk[workflows_sql,auth_aws]

# HTTP workflows with Azure auth  
pip install atlan-application-sdk[workflows_http,auth_azure]

# Full development setup
uv sync --all-extras --group dev --group tests

# Minimal workflow setup
uv sync --extra workflows_http
```

## Timeline

| Version | Status |
|---------|--------|
| v0.3.0 | New groups introduced, old names deprecated |
| v0.4.0 | Old group names will be **removed** |

Please complete your migration before v0.4.0 to avoid breaking changes.
