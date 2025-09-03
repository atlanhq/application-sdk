# Server Code Review Guidelines - FastAPI Applications

## Context-Specific Patterns

This directory contains FastAPI server implementations, middleware, routers, and API endpoint definitions. These components handle HTTP requests, authentication, and API responses.

### Phase 1: Critical Server Safety Issues

**API Security Requirements:**

- All endpoints must have proper input validation using Pydantic models
- Authentication and authorization must be enforced on protected endpoints
- No sensitive data in API responses (passwords, tokens, internal IDs)
- Request rate limiting must be implemented for public endpoints
- CORS configuration must be explicit and restrictive

**Input Validation and Sanitization:**

- All request bodies must use Pydantic models for validation
- Query parameters must be validated with proper types
- File uploads must have size and type restrictions
- SQL injection prevention in any database queries
- No raw user input in log messages

```python
# ✅ DO: Proper input validation
from pydantic import BaseModel, Field, validator

class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, regex="^[a-zA-Z0-9_]+$")
    email: str = Field(..., regex=r'^[\w\.-]+@[\w\.-]+\.\w+$')
    age: int = Field(..., ge=18, le=120)

    @validator('username')
    def username_must_not_contain_prohibited_words(cls, v):
        prohibited = ['admin', 'root', 'system']
        if any(word in v.lower() for word in prohibited):
            raise ValueError('Username contains prohibited words')
        return v

@app.post("/users/", response_model=UserResponse)
async def create_user(user_data: CreateUserRequest):
    # Input is already validated by Pydantic
    return await user_service.create_user(user_data)

# ❌ NEVER: Raw input without validation
@app.post("/users/")
async def bad_create_user(request: dict):  # No validation!
    username = request.get("username")  # Could be anything
    return await user_service.create_user(username)
```

### Phase 2: FastAPI Architecture Patterns

**Router Organization:**

- Group related endpoints in separate router modules
- Use consistent URL patterns and naming conventions
- Implement proper HTTP status codes for all responses
- Use response models for all endpoint returns
- Implement proper error handling with HTTP exceptions

**Dependency Injection:**

- Use FastAPI's dependency injection for database connections
- Implement proper dependency scoping (request, application)
- Create reusable dependencies for authentication, logging, etc.
- Use dependency override for testing
- Implement proper cleanup for dependencies

```python
# ✅ DO: Proper FastAPI router with dependencies
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List

router = APIRouter(prefix="/api/v1/users", tags=["users"])

async def get_db_connection():
    async with database_pool.acquire() as conn:
        try:
            yield conn
        finally:
            # Connection automatically returned to pool
            pass

async def get_current_user(token: str = Depends(oauth2_scheme)):
    user = await auth_service.get_user_from_token(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user

@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncConnection = Depends(get_db_connection)
):
    if user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this user"
        )

    user = await user_service.get_user(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return user
```

### Phase 3: Server Testing Requirements

**API Testing Standards:**

- Use FastAPI's TestClient for endpoint testing
- Test all HTTP status codes (success, client errors, server errors)
- Test authentication and authorization scenarios
- Test input validation with invalid data
- Mock external dependencies in API tests
- Include integration tests with real database

**Request/Response Testing:**

- Test request body validation with Pydantic models
- Test query parameter validation
- Test response model serialization
- Test error response formats
- Test file upload functionality
- Include performance tests for API endpoints

### Phase 4: Performance and Scalability

**API Performance:**

- Use async/await for all I/O operations
- Implement proper database connection pooling
- Use response caching where appropriate
- Implement request batching for bulk operations
- Monitor API response times and error rates

**Middleware and Request Processing:**

- Implement request logging middleware with correlation IDs
- Use compression middleware for large responses
- Implement proper timeout handling for long-running operations
- Use background tasks for non-critical operations
- Monitor memory usage and connection counts

```python
# ✅ DO: Efficient async endpoint with proper error handling
@router.post("/users/bulk", response_model=List[UserResponse])
async def create_users_bulk(
    users_data: List[CreateUserRequest],
    background_tasks: BackgroundTasks,
    db: AsyncConnection = Depends(get_db_connection),
    current_user: User = Depends(get_admin_user)
):
    if len(users_data) > 100:  # Prevent abuse
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot create more than 100 users at once"
        )

    try:
        # Batch operation for better performance
        created_users = await user_service.create_users_batch(db, users_data)

        # Non-critical operation in background
        background_tasks.add_task(
            send_welcome_emails,
            [user.email for user in created_users]
        )

        return created_users

    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation failed: {e}"
        )
    except Exception as e:
        logger.error(f"Bulk user creation failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )
```

### Phase 5: Server Maintainability

**API Documentation and Versioning:**

- Use OpenAPI tags for endpoint organization
- Document all endpoints with proper descriptions
- Implement API versioning strategy
- Use response examples in OpenAPI documentation
- Document all possible error responses

**Configuration and Environment:**

- Externalize all server configuration
- Use environment-specific settings
- Implement proper CORS configuration
- Configure security headers
- Document all configuration options

---

## Server-Specific Anti-Patterns

**Always Reject:**

- Endpoints without input validation
- Missing authentication on protected endpoints
- Raw dictionaries instead of Pydantic models
- Generic exception handling without proper HTTP responses
- Hardcoded configuration values
- Missing CORS configuration
- Endpoints without proper HTTP status codes
- Blocking operations in async endpoints

**Input Validation Anti-Patterns:**

```python
# ❌ REJECT: No input validation
@app.post("/users/")
async def bad_endpoint(data: dict):  # No validation
    username = data["username"]  # Could fail with KeyError
    # No type checking, no sanitization
    return {"status": "created"}

# ✅ REQUIRE: Proper validation with Pydantic
@app.post("/users/", response_model=UserResponse)
async def good_endpoint(user_data: CreateUserRequest):
    # Pydantic automatically validates input
    validated_user = await user_service.create_user(user_data)
    return validated_user
```

**Error Handling Anti-Patterns:**

```python
# ❌ REJECT: Poor error handling
@app.get("/users/{user_id}")
async def bad_get_user(user_id: int):
    try:
        user = await user_service.get_user(user_id)
        return user  # No response model
    except Exception as e:
        return {"error": str(e)}  # Wrong HTTP status, exposes internals

# ✅ REQUIRE: Proper error handling
@app.get("/users/{user_id}", response_model=UserResponse)
async def good_get_user(user_id: int):
    try:
        user = await user_service.get_user(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        return user
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid request: {e}"
        )
    except Exception as e:
        logger.error(f"User retrieval failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )
```

## Educational Context for Server Reviews

When reviewing server code, emphasize:

1. **Security Impact**: "API endpoints are the primary attack surface. Proper input validation and authentication aren't just good practices - they're essential for preventing data breaches and unauthorized access."

2. **Performance Impact**: "Server performance directly affects user experience. Blocking operations in async endpoints can cause cascading slowdowns that affect all API users."

3. **Reliability Impact**: "Proper error handling in APIs determines whether clients can gracefully handle failures or crash unexpectedly. Clear error responses help clients implement proper retry logic."

4. **Maintainability Impact**: "Well-structured FastAPI applications with proper dependency injection and router organization make it easier for teams to add features and maintain the codebase as it grows."

5. **Observability Impact**: "API logging and monitoring are critical for debugging production issues. Proper request correlation IDs and structured logging make the difference between quick problem resolution and extended outages."
