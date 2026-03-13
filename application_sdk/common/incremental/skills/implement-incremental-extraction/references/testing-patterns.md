# Testing Patterns for Incremental Extraction

This reference covers unit testing patterns for incremental extraction implementations.

## What to Test

| Component | Test? | Reason |
|-----------|-------|--------|
| `build_incremental_column_sql()` | YES | Business logic - SQL generation |
| `resolve_database_placeholders()` | YES | Business logic - placeholder replacement |
| `execute_column_batch()` | NO | Concrete in SDK, don't override |
| `fetch_tables()` | NO | SDK handles, test via integration |
| `fetch_incremental_marker()` | NO | SDK handles, test via integration |

## Test Structure

```python
"""Unit tests for YourDB-specific column extraction logic.

Tests cover YourDB's build_incremental_column_sql implementation which generates
database-specific SQL syntax for incremental column extraction.

Generic column extraction tests (get_backfill_tables, get_transformed_dir)
live in the SDK: tests/unit/common/test_column_extraction.py
"""

import pytest
from app.activities.metadata_extraction.your_db import YourDBActivities


class TestBuildIncrementalColumnSql:
    """Tests for YourDB-specific build_incremental_column_sql method."""

    def _make_activities(self, template_sql):
        """Create a YourDBActivities instance for testing without __init__."""
        obj = YourDBActivities.__new__(YourDBActivities)
        obj.incremental_column_sql = template_sql
        return obj

    def _build(self, template, table_ids, schema_name, marker):
        """Helper to call build_incremental_column_sql with mock workflow_args."""
        activities = self._make_activities(template)
        workflow_args = {
            "metadata": {
                "marker_timestamp": marker,
                "next_marker_timestamp": marker,
                "system_schema_name": schema_name,
                "incremental-extraction": True,
            }
        }
        return activities.build_incremental_column_sql(table_ids, workflow_args)
```

## Essential Test Cases

### 1. Basic SQL Generation

```python
def test_builds_sql_with_multiple_tables(self):
    """Test SQL generation with multiple table IDs."""
    template = "--TABLE_FILTER_CTE--\nSELECT * FROM cols"
    table_ids = ["DB.SCH.T1", "DB.SCH.T2", "DB.SCH.T3"]

    result = self._build(template, table_ids, "SYS", "2024-01-01")

    # Verify CTE structure (Oracle pattern)
    assert "WITH table_filter AS" in result
    assert "SELECT 'DB.SCH.T1' AS TABLE_ID FROM dual" in result
    assert "UNION ALL SELECT 'DB.SCH.T2' FROM dual" in result
```

### 2. Single Table (No UNION ALL)

```python
def test_single_table_no_union(self):
    """Test single table generates simple query without UNION ALL."""
    template = "--TABLE_FILTER_CTE--\nSELECT *"
    table_ids = ["DB.SCH.SINGLE_TABLE"]

    result = self._build(template, table_ids, "SYS", "2024-01-01")

    assert "WITH table_filter AS" in result
    assert "UNION ALL" not in result
```

### 3. Placeholder Replacement

```python
def test_replaces_all_placeholders(self):
    """Test all placeholder variants are replaced."""
    template = (
        "--TABLE_FILTER_CTE--\n"
        "WHERE s = :schema_name AND t > :marker_timestamp "
        "AND sys = {system_schema} AND m = {marker_timestamp}"
    )

    result = self._build(template, ["T1"], "MY_SCHEMA", "2024-12-31")

    assert ":schema_name" not in result
    assert ":marker_timestamp" not in result
    assert "{system_schema}" not in result
    assert "MY_SCHEMA" in result
    assert "2024-12-31" in result
```

### 4. Empty Table List Raises Error

```python
def test_empty_table_list_raises(self):
    """Test that empty table list raises ValueError."""
    with pytest.raises(ValueError, match="No table IDs provided"):
        self._build("--TABLE_FILTER_CTE--", [], "SCH", "2024-01-01")
```

### 5. SQL Injection Prevention (Quote Escaping)

```python
def test_escapes_single_quotes_in_table_names(self):
    """Test single quotes in table IDs are escaped for SQL safety."""
    template = "--TABLE_FILTER_CTE--\nSELECT *"
    table_ids = ["DB.SCH.O'Brien", "DB.SCH.TEST'TABLE"]

    result = self._build(template, table_ids, "SCH", "2024-01-01")

    # Single quotes should be doubled for SQL escaping
    assert "O''Brien" in result
    assert "TEST''TABLE" in result
```

### 6. Special Characters Preserved

```python
def test_preserves_special_chars_in_table_names(self):
    """Test database-specific special characters are preserved."""
    template = "--TABLE_FILTER_CTE--\nSELECT *"
    # Oracle allows $ and # in identifiers
    table_ids = ["DB.SCH.TABLE$NAME", "DB.SCH.TABLE#2"]

    result = self._build(template, table_ids, "SCH", "2024-01-01")

    assert "TABLE$NAME" in result
    assert "TABLE#2" in result
```

### 7. Large Batch (Performance)

```python
def test_long_table_list(self):
    """Test handling of large table list (performance/correctness)."""
    template = "--TABLE_FILTER_CTE--\nSELECT *"
    table_ids = [f"DB.SCHEMA.TABLE_{i}" for i in range(100)]

    result = self._build(template, table_ids, "SYS", "2024-01-01")

    assert "SELECT 'DB.SCHEMA.TABLE_0' AS TABLE_ID FROM dual" in result
    assert result.count("UNION ALL") == 99
    assert "TABLE_99" in result
```

### 8. Template Structure Preservation

```python
def test_preserves_template_structure(self):
    """Test template structure is preserved after CTE injection."""
    template = """--TABLE_FILTER_CTE--
SELECT c.* FROM {system_schema}.DBA_TAB_COLS c
JOIN table_filter tf ON tf.TABLE_ID = c.OWNER || '.' || c.TABLE_NAME
WHERE c.LAST_DDL_TIME >= TO_TIMESTAMP('{marker_timestamp}')
ORDER BY c.OWNER, c.TABLE_NAME"""
    table_ids = ["DB.SCHEMA.TABLE1"]

    result = self._build(template, table_ids, "CUSTOM_SYS", "2024-06-15T00:00:00Z")

    assert result.startswith("WITH table_filter AS")
    assert "CUSTOM_SYS.DBA_TAB_COLS" in result
    assert "JOIN table_filter tf ON" in result
    assert "ORDER BY c.OWNER, c.TABLE_NAME" in result
```

## Test for resolve_database_placeholders

```python
class TestResolveDatabasePlaceholders:
    """Tests for database-specific placeholder resolution."""

    def _make_activities(self):
        obj = YourDBActivities.__new__(YourDBActivities)
        return obj

    def test_replaces_system_schema(self):
        activities = self._make_activities()
        sql = "SELECT * FROM {system_schema}.DBA_TABLES"
        workflow_args = {
            "metadata": {"system_schema_name": "CUSTOM_SYS"}
        }

        result = activities.resolve_database_placeholders(sql, workflow_args)

        assert result == "SELECT * FROM CUSTOM_SYS.DBA_TABLES"

    def test_no_op_when_no_placeholders(self):
        activities = self._make_activities()
        sql = "SELECT * FROM system.tables"
        workflow_args = {"metadata": {}}

        result = activities.resolve_database_placeholders(sql, workflow_args)

        assert result == sql
```

## Running Tests

```bash
# Run specific test file
uv run pytest tests/unit/test_column_utils.py -v

# Run with coverage
uv run pytest tests/unit/test_column_utils.py -v --cov=app

# Note: Tests may fail with ImportError if the SDK's incremental module
# is not installed (only available in SDK >= X.Y.Z with [incremental] extra).
# This is expected in CI until the SDK version is released.
```

## Common Test Mistakes

1. **Calling old method name**: Always use `build_incremental_column_sql()`, not `_build_column_cte_sql` or any private name
2. **Not using `__new__`**: Use `YourDBActivities.__new__(YourDBActivities)` to create instances without `__init__` side effects
3. **Testing SDK methods**: Don't test `execute_column_batch`, `run_column_query`, etc. - those are SDK's responsibility
4. **Forgetting `{marker_timestamp}` in assertions**: The SDK resolves this, not your method, so it may still be present after `build_incremental_column_sql`
