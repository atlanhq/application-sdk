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

### Phase 5: Client Maintainability

**Code Organization:**

- Separate client interface from implementation
- Use dependency injection for client configuration
- Implement proper logging with connection context
- Document connection parameters and requirements
- Follow consistent error handling patterns

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
