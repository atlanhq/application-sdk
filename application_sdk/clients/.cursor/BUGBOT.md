# Client Code Review Guidelines - Database and External Services

## Context-Specific Patterns

This directory contains database clients, external service clients, and connection management code. These components are critical for data integrity, performance, and security.

### Phase 1: Critical Client Safety Issues

**Database Connection Security:**

- SQL injection prevention through parameterized queries ONLY
- Connection strings must never contain hardcoded credentials
- Database passwords must be retrieved from secure credential stores
- SSL/TLS required for all external database connections
- Connection timeouts must be explicitly configured

**Example SQL Injection Prevention:**

```python
# ✅ DO: Parameterized queries
cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))

# ❌ NEVER: String concatenation
cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
```

### Phase 2: Client Architecture Patterns

**Connection Pooling Requirements:**

- All database clients MUST use connection pooling
- Pool size must be configurable via environment variables
- Connection validation on checkout required
- Proper connection cleanup in finally blocks
- Connection leak detection in development/testing

**Class Responsibility Separation:**

- **Always flag multi-responsibility classes**: Classes handling both client functionality and domain-specific logic must be separated
- **Client vs Logic separation**: Database clients should handle connections, not business rules
- **Extract domain logic**: Lock management, caching, or processing logic should be in separate classes
- **Single purpose interfaces**: Each client class should have one clear responsibility

```python
# ❌ REJECT: Mixed responsibilities
class RedisClient:
    def connect(self):
        """Handle connection setup"""
    def acquire_lock(self, lock_name):
        """Lock functionality - should be separate"""
    def get_data(self, key):
        """Client functionality"""

# ✅ REQUIRE: Separated responsibilities
class RedisClient:
    def connect(self):
        """Handle connection setup"""
    def get_data(self, key):
        """Client functionality"""

class RedisLockManager:  # Separate class for lock functionality
    def __init__(self, client: RedisClient):
        self.client = client
    def acquire_lock(self, lock_name):
        """Lock-specific logic"""
```

**Async Client Patterns:**

- Use async/await for all I/O operations
- Implement proper connection context managers
- Handle connection failures gracefully with retries
- Use asyncio connection pools, not synchronous pools

```python
# ✅ DO: Proper async connection management
async def execute_query(self, query: str, params: tuple):
    async with self.pool.acquire() as conn:
        try:
            return await conn.fetch(query, *params)
        except Exception as e:
            logger.error(f"Query failed: {query[:100]}...", exc_info=True)
            raise
```

**Configuration Management for Clients:**

- **Environment-specific settings**: All connection parameters must be externalized to environment variables
- **Default value validation**: Every configuration parameter must have a sensible default and validation
- **Development vs Production**: Client configurations must work in both environments
- **Configuration consolidation**: Related configuration should be grouped together

```python
# ✅ DO: Proper client configuration
class DatabaseClientConfig:
    def __init__(self):
        self.host = os.getenv("DB_HOST", "localhost")
        self.port = int(os.getenv("DB_PORT", "5432"))
        self.max_connections = int(os.getenv("DB_MAX_CONNECTIONS", "20"))
        self.timeout = int(os.getenv("DB_TIMEOUT_SECONDS", "30"))
        self.ssl_required = os.getenv("DB_SSL_REQUIRED", "true").lower() == "true"
        self._validate()

    def _validate(self):
        if self.max_connections <= 0:
            raise ValueError("DB_MAX_CONNECTIONS must be positive")
        if self.timeout <= 0:
            raise ValueError("DB_TIMEOUT_SECONDS must be positive")

# ❌ REJECT: Poor configuration management
class BadDatabaseClient:
    def __init__(self):
        self.host = "localhost"  # Hardcoded
        self.connections = os.getenv("MAX_CONN")  # No default, no validation
```

### Phase 3: Client Testing Requirements

**Database Client Testing:**

- Mock database connections in unit tests
- Use test databases for integration tests
- Test connection failure scenarios
- Verify connection pool behavior
- Test query parameter sanitization
- Include performance tests for connection pooling

**External Service Client Testing:**

- Mock external APIs in unit tests
- Test timeout and retry behaviors
- Test authentication failure scenarios
- Include circuit breaker tests
- Verify proper error handling and logging

### Phase 4: Performance and Scalability

**Query Performance:**

- Flag SELECT \* queries without LIMIT
- Require WHERE clauses on indexed columns
- Batch operations when possible
- Use prepared statements for repeated queries
- Monitor and limit query execution time

**Connection Management Performance:**

- Connection pool size must match expected concurrency
- Connection validation queries must be lightweight
- Implement connection health checks
- Use connection keepalive for long-running connections
- Monitor connection pool metrics

**Resource Limit Validation:**

- **Key length constraints**: Validate Redis key lengths against limits (typically 512MB max)
- **Connection limits**: Ensure connection pool sizes don't exceed database limits
- **Query complexity**: Monitor and limit expensive query execution time
- **Memory constraints**: Validate result set sizes for large queries

```python
# ✅ DO: Resource validation
def validate_redis_key(key: str) -> str:
    if len(key.encode('utf-8')) > 512 * 1024 * 1024:  # 512MB limit
        raise ValueError(f"Redis key too long: {len(key)} bytes")
    return key

def create_lock_key(application: str, resource: str, run_id: str) -> str:
    key = f"{application}:{resource}:{run_id}"
    return validate_redis_key(key)
```

### Phase 5: Client Maintainability

**Code Organization:**

- Separate client interface from implementation
- Use dependency injection for client configuration
- Implement proper logging with connection context
- Document connection parameters and requirements
- Follow consistent error handling patterns

**Error Handling Improvements:**

- **Comprehensive try-catch blocks**: All client operations that can fail must be wrapped in try-catch blocks
- **SDK-specific exceptions**: Use `ClientError` from `application_sdk/common/error_codes.py` instead of generic exceptions
- **Operation context**: Include operation details (query, connection info) in error messages
- **Retry vs fail-fast**: Distinguish between retryable connection errors and permanent failures

```python
# ✅ DO: Comprehensive error handling
from application_sdk.common.error_codes import ClientError

async def execute_query(self, query: str, params: tuple = ()) -> list:
    try:
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *params)
    except ConnectionRefusedError as e:
        # Retryable error
        logger.warning(f"Database connection refused, will retry: {e}")
        raise ClientError(f"Database temporarily unavailable: {e}")
    except ValidationError as e:
        # Non-retryable error
        logger.error(f"Query validation failed: {query[:50]}...")
        raise ClientError(f"Invalid query: {e}")
    except Exception as e:
        logger.error(f"Unexpected database error: {e}", exc_info=True)
        raise ClientError(f"Database operation failed: {e}")
```

**Configuration Management:**

- Externalize all connection parameters
- Support multiple environment configurations
- Implement configuration validation
- Use secure credential management
- Document all configuration options

---

## Client-Specific Anti-Patterns

**Always Reject:**

- Hardcoded connection strings or credentials
- Missing connection timeouts
- Synchronous database calls in async contexts
- SQL queries built through string concatenation
- Connection objects stored as instance variables
- Missing connection pool cleanup
- Generic exception handling without context
- Direct database connections without pooling

**Configuration Anti-Patterns:**

- **Missing environment variables**: Parameters that should be configurable but are hardcoded
- **No validation**: Environment variables used without type checking or range validation
- **Missing defaults**: Required configuration without sensible fallback values
- **Environment inconsistency**: Features that work in development but fail in production

**Connection Management Anti-Patterns:**

```python
# ❌ REJECT: Poor connection management
class BadSQLClient:
    def __init__(self):
        self.conn = psycopg2.connect("host=localhost...")  # No pooling

    def query(self, sql):
        cursor = self.conn.cursor()
        cursor.execute(sql)  # No parameterization
        return cursor.fetchall()  # No cleanup

# ✅ REQUIRE: Proper connection management
class GoodSQLClient:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    async def query(self, sql: str, params: tuple = ()):
        async with self.pool.acquire() as conn:
            try:
                return await conn.fetch(sql, *params)
            finally:
                # Connection automatically returned to pool
                pass
```

## Educational Context for Client Reviews

When reviewing client code, emphasize:

1. **Security Impact**: "Database clients are the primary attack vector for SQL injection. Parameterized queries aren't just best practice - they're essential for protecting enterprise customer data."

2. **Performance Impact**: "Connection pooling isn't optional at enterprise scale. Creating new connections for each query can overwhelm database servers and create bottlenecks that affect all users."

3. **Reliability Impact**: "Proper error handling in clients determines whether temporary network issues cause cascading failures or graceful degradation."

4. **Maintainability Impact**: "Client abstraction layers allow us to change databases or connection strategies without affecting business logic throughout the application."

5. **Configuration Impact**: "Externalized configuration enables the same code to work across development, staging, and production environments. Missing this leads to environment-specific bugs that are hard to reproduce and fix."
