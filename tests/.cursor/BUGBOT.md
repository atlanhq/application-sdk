# Testing Code Review Guidelines - Test Implementation Standards

## Context-Specific Patterns

This directory contains all test implementations for the Application SDK. Tests must be reliable, maintainable, and provide confidence in code correctness without being brittle.

### Phase 1: Critical Testing Safety Issues

**Test Reliability Requirements:**

- All tests must be deterministic - same code always produces same test results
- No tests should depend on external services (real databases, APIs, network)
- Tests must not have hidden dependencies on execution order
- No shared mutable state between test cases
- Tests must clean up any resources they create

**Test Data Safety:**

- No real customer data or production data in tests
- Test databases must be isolated and disposable
- Mock sensitive operations (email sending, payment processing)
- No hardcoded secrets or credentials in test code
- Test data must be anonymized and safe for version control

**Mock Accuracy Requirements:**

- **Exact method name matching**: Mock patches must match actual method names precisely
- **API signature consistency**: Mocked methods must have signatures matching real implementations
- **Return type matching**: Mock return values must match actual method return types
- **Patch target accuracy**: Mock patches must target the correct module and method paths

```python
# ✅ DO: Accurate mocking
from unittest.mock import AsyncMock, patch

class TestObjectStoreUpload:
    """Test object store functionality with correct mocking."""

    @patch('application_sdk.services.objectstore.ObjectStore.upload_file')  # Correct method name
    async def test_file_upload_success(self, mock_upload):
        """Test successful file upload with proper mock."""

        # Mock return value matches actual method signature
        mock_upload.return_value = {"status": "success", "key": "test_file.json"}

        output = JsonOutput()
        result = await output.upload_data([{"test": "data"}], "test_path")

        # Verify mock was called correctly
        mock_upload.assert_called_once()
        call_args = mock_upload.call_args
        assert "test_path" in call_args[0]  # Verify path parameter

# ❌ NEVER: Incorrect mocking
class BadTestExample:
    @patch('application_sdk.services.ObjectStore.upload')  # Wrong method name!
    async def test_bad_upload(self, mock_upload):
        """This test will fail because method name is wrong."""
        # Mock won't intercept calls to 'upload_file' method
        pass
```

**Test Isolation and Cleanup:**

```python
# ✅ DO: Proper test isolation and cleanup
import pytest
from unittest.mock import AsyncMock, patch

class TestUserService:
    """Test suite for UserService with proper isolation."""

    @pytest.fixture
    async def test_db(self):
        """Create isolated test database connection."""
        db = await create_test_database()
        try:
            yield db
        finally:
            await cleanup_test_database(db)

    @pytest.fixture
    def user_service(self, test_db):
        """Create UserService with test dependencies."""
        return UserService(database=test_db)

    async def test_create_user_success(self, user_service):
        """Test successful user creation with valid data."""
        # Arrange
        user_data = CreateUserRequest(
            username="testuser",
            email="test@example.com",
            age=25
        )

        # Act
        result = await user_service.create_user(user_data)

        # Assert
        assert result.id is not None
        assert result.username == "testuser"
        assert result.email == "test@example.com"

# ❌ NEVER: Tests with external dependencies
class BadTestExample:
    async def test_send_email(self):
        # This actually sends emails!
        result = await email_service.send_email("test@example.com", "Test")
        assert result.success
```

### Phase 2: Test Architecture Patterns

**Test Organization:**

- Mirror source code structure in test directory layout
- Group related tests in classes with descriptive names
- Use consistent naming: `test_<function>_<scenario>`
- Separate unit, integration, and e2e tests clearly
- One test file per source file being tested

**Mocking and Fixtures:**

- Use pytest fixtures for common test setup
- Mock external dependencies, not internal business logic
- Use hypothesis for property-based testing
- Create reusable test factories for complex objects
- Implement proper teardown for all fixtures

**Mock Verification Patterns:**

- **Method name validation**: Always verify that mocked method names exist in the actual class
- **Parameter validation**: Check that mocked methods are called with expected parameters
- **Call count verification**: Verify methods are called the expected number of times
- **Return value consistency**: Ensure mock return values match production behavior

```python
# ✅ DO: Comprehensive mock verification
class TestSQLMetadataExtractor:
    """Test suite for SQL metadata extraction functionality."""

    @pytest.fixture
    async def mock_sql_client(self):
        """Mock SQL client with controlled responses."""
        client = AsyncMock()
        # Return value matches actual method signature
        client.execute_query.return_value = [
            {"table_name": "users", "column_count": 5},
            {"table_name": "orders", "column_count": 8}
        ]
        return client

    @pytest.fixture
    def metadata_extractor(self, mock_sql_client):
        """Create extractor with mocked dependencies."""
        return SQLMetadataExtractor(sql_client=mock_sql_client)

    async def test_extract_table_metadata_success(self, metadata_extractor, mock_sql_client):
        """Test successful extraction of table metadata."""
        # Arrange
        database_name = "test_db"

        # Act
        result = await metadata_extractor.extract_tables(database_name)

        # Assert - Verify results
        assert len(result) == 2
        assert result[0].name == "users"
        assert result[1].name == "orders"

        # Assert - Verify mock interactions
        mock_sql_client.execute_query.assert_called_once()
        call_args = mock_sql_client.execute_query.call_args
        assert database_name in str(call_args)

    @given(st.text(min_size=1, max_size=50))
    async def test_extract_with_various_database_names(self, metadata_extractor, database_name):
        """Test metadata extraction with various database names using property-based testing."""
        # Property: Should not raise exceptions with valid database names
        try:
            result = await metadata_extractor.extract_tables(database_name)
            assert isinstance(result, list)
        except ValueError:
            # Acceptable for invalid database names
            pass
```

### Phase 3: Test Quality Standards

**Test Coverage and Completeness:**

- New code must have corresponding tests
- Test both success and failure scenarios
- Include edge cases and boundary conditions
- Test error handling and exception cases
- Cover all branches of conditional logic
- Use coverage reports to identify gaps

**Test Assertions and Verification:**

- Use specific assertions, not generic `assert result`
- Test both return values and side effects
- Verify mock interactions when testing behavior
- Include negative test cases (what should NOT happen)
- Test async code properly with `pytest-asyncio`

**Test Method Accuracy:**

- **Verify mocked method existence**: Before writing tests, confirm that the mocked method actually exists
- **Check method signatures**: Ensure test calls match actual method signatures
- **Validate return types**: Mock return values should match actual method return types
- **Update tests with API changes**: When APIs change, update corresponding mocks

```python
# ✅ DO: Comprehensive test with proper assertions and mock accuracy
class TestFileUploadOperations:
    """Test file upload operations with accurate mocking."""

    # Verify this matches the actual ObjectStore.upload_file method signature
    @patch('application_sdk.services.objectstore.ObjectStore.upload_file')
    async def test_upload_multiple_files_success(self, mock_upload_file):
        """Test successful upload of multiple files."""

        # Mock return value matches actual method
        mock_upload_file.return_value = {"status": "success", "bytes_uploaded": 1024}

        # Arrange
        files = ["file1.json", "file2.json", "file3.json"]
        uploader = FileUploader()

        # Act
        results = await uploader.upload_files(files)

        # Assert - Verify results
        assert len(results) == 3
        assert all(r["status"] == "success" for r in results)

        # Assert - Verify mock interactions
        assert mock_upload_file.call_count == 3

        # Verify each call had correct parameters
        for i, call in enumerate(mock_upload_file.call_args_list):
            args, kwargs = call
            assert files[i] in str(args)

# ❌ DON'T: Inaccurate mocking
class BadUploadTest:
    @patch('application_sdk.services.ObjectStore.upload')  # Wrong method name!
    async def test_bad_upload_mocking(self, mock_upload):
        """This test won't work because 'upload' method doesn't exist."""

        uploader = FileUploader()
        await uploader.upload_files(["test.json"])

        # This assertion will fail because the wrong method was mocked
        mock_upload.assert_called_once()
```

**Comprehensive Error Testing:**

```python
# ✅ DO: Comprehensive test with proper assertions
async def test_user_creation_with_duplicate_email(self, user_service, test_db):
    """Test that duplicate email addresses are rejected."""
    # Arrange
    email = "duplicate@example.com"
    first_user = CreateUserRequest(username="user1", email=email, age=25)
    second_user = CreateUserRequest(username="user2", email=email, age=30)

    # Act & Assert - First user should succeed
    result1 = await user_service.create_user(first_user)
    assert result1.id is not None

    # Second user with same email should fail
    with pytest.raises(ValidationError) as exc_info:
        await user_service.create_user(second_user)

    # Verify specific error details
    assert "email" in str(exc_info.value)
    assert "already exists" in str(exc_info.value).lower()

    # Verify no user was created
    users_with_email = await user_service.find_by_email(email)
    assert len(users_with_email) == 1  # Only first user

# ❌ DON'T: Weak assertions
async def test_bad_user_creation(self, user_service):
    result = await user_service.create_user(some_data)
    assert result  # Too vague - what are we checking?
```

### Phase 4: Performance and Integration Testing

**Performance Test Requirements:**

- Include performance tests for critical operations
- Set realistic performance expectations
- Test memory usage for large dataset operations
- Include load tests for concurrent operations
- Monitor resource cleanup in performance tests

**Integration Test Patterns:**

- Use real database instances (not production data)
- Test actual API endpoints with TestClient
- Test workflow and activity integration
- Include database transaction rollback tests
- Test external service integrations with controlled environments

### Phase 5: Test Maintainability

**Test Documentation and Clarity:**

- Test names must clearly describe the scenario
- Include docstrings explaining complex test logic
- Document any test data requirements
- Explain why specific mocking approaches are used
- Keep test code as readable as production code

**Test Environment Management:**

- Use separate test configuration
- Implement proper test data factories
- Create utilities for common test operations
- Manage test database schemas consistently
- Document test environment setup requirements

---

## Testing-Specific Anti-Patterns

**Always Reject:**

- Tests that call real external services
- Tests with non-deterministic behavior
- Tests that depend on execution order
- Shared mutable state between tests
- Tests without proper cleanup
- Overly complex test setup
- Tests that test implementation details instead of behavior
- Missing test documentation

**Mock Accuracy Anti-Patterns:**

- **Wrong method names**: Mocking methods that don't exist or have incorrect names
- **Incorrect patch targets**: Patching the wrong module or class path
- **Signature mismatches**: Mock calls that don't match actual method signatures
- **Return type inconsistencies**: Mock return values that don't match actual types

**Test Reliability Anti-Patterns:**

```python
# ❌ REJECT: Non-deterministic test
def test_bad_random_behavior():
    # Uses actual random values
    result = generate_user_id()
    assert len(result) == 10  # Could fail randomly

# ❌ REJECT: Test depends on external service
async def test_bad_api_integration():
    response = await requests.get("https://api.example.com/users")
    assert response.status_code == 200  # Could fail due to network

# ❌ REJECT: Incorrect mocking
@patch('application_sdk.services.ObjectStore.upload')  # Method doesn't exist!
def test_bad_mock_name(mock_upload):
    result = object_store.upload_file("test.json")  # Calls 'upload_file', not 'upload'
    mock_upload.assert_called_once()  # Will fail

# ❌ REJECT: Shared state between tests
user_counter = 0

def test_bad_shared_state_1():
    global user_counter
    user_counter += 1
    assert user_counter == 1  # Fails if test order changes

def test_bad_shared_state_2():
    global user_counter
    user_counter += 1
    assert user_counter == 2  # Brittle dependency

# ✅ REQUIRE: Deterministic, isolated tests with accurate mocking
@pytest.fixture
def test_user_factory():
    """Factory for creating test users with predictable data."""
    counter = 0
    def create_user(username=None, email=None):
        nonlocal counter
        counter += 1
        return CreateUserRequest(
            username=username or f"testuser_{counter}",
            email=email or f"test_{counter}@example.com",
            age=25
        )
    return create_user

@patch('application_sdk.services.objectstore.ObjectStore.upload_file')  # Correct method name
def test_good_isolated_behavior(mock_upload_file, test_user_factory):
    """Test user creation with isolated test data and accurate mocking."""
    mock_upload_file.return_value = {"status": "success", "key": "user_data.json"}

    user = test_user_factory()
    assert user.username.startswith("testuser_")
    assert "@example.com" in user.email

    # Test actual upload functionality
    result = upload_user_data(user)
    mock_upload_file.assert_called_once()
    assert result["status"] == "success"
```

**Test Mocking Anti-Patterns:**

```python
# ❌ REJECT: Over-mocking internal logic
@patch('user_service.validate_email')  # Don't mock internal logic
@patch('user_service.hash_password')   # Don't mock internal logic
async def test_bad_overmocked_user_creation():
    # This test tells us nothing about real behavior
    pass

# ✅ REQUIRE: Mock external dependencies only
@patch('email_client.send_welcome_email')  # Mock external service
@patch('application_sdk.services.objectstore.ObjectStore.upload_file')  # Correct method name
async def test_good_user_creation_sends_email(mock_email, mock_upload, user_service):
    """Test that user creation triggers welcome email."""
    mock_upload.return_value = {"status": "success"}
    mock_email.return_value = {"delivered": True}

    user_data = CreateUserRequest(username="test", email="test@example.com", age=25)

    result = await user_service.create_user(user_data)

    # Verify the business behavior
    assert result.id is not None
    mock_email.assert_called_once_with("test@example.com", result.id)
    mock_upload.assert_called()  # Verify data was stored
```

## Educational Context for Test Reviews

When reviewing test code, emphasize:

1. **Reliability Impact**: "Flaky tests undermine confidence in the entire test suite. Tests that sometimes pass and sometimes fail train developers to ignore test failures, defeating the purpose of testing."

2. **Maintainability Impact**: "Tests are code that needs to be maintained. Overly complex test setup or brittle mocking makes tests harder to update when requirements change, slowing down development."

3. **Coverage vs Quality**: "High test coverage with poor test quality provides false confidence. Tests should verify behavior, not just exercise code paths."

4. **Feedback Speed**: "Fast, reliable tests enable rapid development cycles. Tests that take too long to run or require complex setup discourage developers from running them frequently."

5. **Documentation Value**: "Well-written tests serve as executable documentation of system behavior. They should clearly show how components are intended to work and what edge cases are handled."

6. **Mock Accuracy Impact**: "Incorrect mocking creates false confidence. Tests that mock non-existent methods or wrong signatures can pass while the real code fails. Always verify that mocks match actual implementations."

## Test Command Reference

Run tests using: `uv run coverage run -m pytest --import-mode=importlib --capture=no --log-cli-level=INFO tests/ -v --full-trace --hypothesis-show-statistics`
