# Write Tests

Create or update tests for SDK components.

## Source of Truth

Read these before starting:
- `docs/agents/testing.md` — test guidelines, coverage targets
- `.cursor/rules/testing.mdc` — detailed testing framework and patterns

## Reference Implementations

Study existing tests in `tests/unit/` — they mirror the source structure.

## Instructions

1. **Create test file** at `tests/unit/{mirror_path}/test_{filename}.py` matching the source file's location
2. **Use pytest** with AAA pattern (Arrange, Act, Assert):
   ```python
   async def test_fetch_returns_data(mock_client):
       # Arrange
       mock_client.get.return_value = {"data": [1, 2, 3]}

       # Act
       result = await handler.fetch_metadata()

       # Assert
       assert result == {"data": [1, 2, 3]}
   ```
3. **Mock external dependencies** — network calls, Dapr services, Temporal context
4. **Use `@pytest.mark.asyncio`** for all async test functions
5. **Create fixtures** in `conftest.py` for shared test setup
6. **Target 85% coverage** for new code
7. **Run tests**: `uv run coverage run -m pytest --import-mode=importlib --capture=no --log-cli-level=INFO tests/ -v --full-trace`
8. **Verify coverage**: `uv run coverage report --show-missing`

$ARGUMENTS
