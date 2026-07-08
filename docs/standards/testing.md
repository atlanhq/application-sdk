# Testing Guidelines

- Run the unit tests using the command: `uv run coverage run -m pytest --import-mode=importlib --capture=no --log-cli-level=INFO tests/ -v --full-trace --hypothesis-show-statistics`


- **Test Framework**
    - Write tests before fixing bugs
    - Keep tests readable and maintainable
    - Test edge cases and error conditions
    - Use pytest as the primary testing framework
    - Use hypothesis for property-based testing
    - All tests must be deterministic
    - Use fixtures for common setup/teardown

- **Test Organization**
    - Unit tests: `tests/unit/`
    - Integration tests: `tests/integration/`
    - End-to-end tests: `tests/e2e/`
    - UI tests (Playwright): `tests/ui/` — mandatory only for the top connectors
      (Snowflake, Databricks, Tableau, DBT and their minors); optional elsewhere
    - Test files should mirror the source structure
    - Test file names should start with `test_`

- **Connector-App Testing Tiers (Agreed Architecture)**

  For apps built on this SDK, the four tiers above map to a specific bar,
  enforced (at WARN tier today) by the conformance suite's T-series
  (`packages/conformance/conformance/docs/rules/tests.md`):

  - **Unit** — helper functions and activities, method by method. The
    universal floor: every app has one, no exceptions (T010).
  - **Integration** — connects to the real source, runs the app's extract
    only (no system apps). Required for every app; covers most scenario
    variations (auth modes, schema shapes, include/exclude filters) since
    the SDK provides hermetic paths for it (embedded Temporal, testcontainers,
    mocked infra) (T011).
  - **End-to-end** — the full pipeline including system apps, in SDR mode
    against a real tenant. Needs only one representative run, not
    scenario-level coverage (T012).
  - **UI** — see above; front-end rendering bugs (lineage display,
    description fields) belong to the front-end team, so one connector
    proving the UI renders is sufficient — the rest validate data via API.

  A scaffold/minimal app with nothing to exercise at the integration or e2e
  tier yet can opt out per-tier in its own `pyproject.toml` (`atlan.yaml` is
  generated from the app's Pkl contract and must not be hand-edited):

  ```toml
  [tool.conformance]
  exempt_test_tiers = ["integration", "e2e"]
  ```

  The conformance suite also checks that tests are *meaningful*, not just
  present — a test that runs without asserting an outcome (`AssertionFreeTest`,
  `EmptyTestBody`, `VacuousAssertion`), a test file pytest silently never
  collects (`UncollectableTestFile`), and a coverage gate that's configured
  but can never fail (`CoverageGateDisabled`) are all flagged. See the T-series
  docs for the full rule catalog and remediation guidance.

- **Example Test Structure**
  ```python
  # DO: Proper test structure
  import pytest
  from hypothesis import given, strategies as st

  class TestUserService:
      """Test suite for UserService."""

      @pytest.fixture
      def user_service(self):
          """Create a UserService instance for testing."""
          return UserService()

      def test_get_user_success(self, user_service):
          """Test successful user retrieval."""
          # Arrange
          user_id = "123"

          # Act
          result = user_service.get_user(user_id)

          # Assert
          assert result is not None
          assert result.id == user_id

      @given(st.text(min_size=1))
      def test_get_user_with_hypothesis(self, user_service, user_id):
          """Test user retrieval with property-based testing."""
          result = user_service.get_user(user_id)
          assert result is not None

      # DON'T: Improper test structure
      def test_get_user(self):  # No docstring
          service = UserService()  # No fixture
          assert service.get_user("123")  # No proper assertion
  ```

- **Test Writing Guidelines**
  - Each test should focus on a single aspect
  - Use descriptive test names
  - Include docstrings for test functions
  - Mock external dependencies

- **Test Categories**
  - Unit Tests: Test individual components in isolation
  - Integration Tests: Test component interactions
  - End-to-End Tests: Test complete workflows
  - Performance Tests: Test system performance
  - Security Tests: Test security aspects

- **Mocking Guidelines**
  - Use pytest-mock for mocking
  - Mock external services and APIs
  - Mock database operations
  - Mock time-dependent operations
  - Mock random number generation

- **Test Data Management**
  - Keep test data minimal and focused
  - Use factories for complex objects
  - Clean up test data after tests
