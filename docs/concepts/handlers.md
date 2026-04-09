# Handlers

Handlers implement the API contract for your application's HTTP endpoints: authentication testing, preflight checks, and metadata browsing. In v3, handlers use the `Handler` ABC with typed contracts and automatic context injection, replacing v2's `HandlerInterface` with its untyped `*args/**kwargs` signatures and manual `load()` method.

## Defining a Handler

```python
from application_sdk.handler import Handler
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    PreflightInput, PreflightOutput, PreflightStatus,
    MetadataInput, MetadataOutput, MetadataField,
)

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        api_key = await self.context.get_secret("my-api-key")
        ok = await verify_key(api_key)
        return AuthOutput(
            status=AuthStatus.SUCCESS if ok else AuthStatus.FAILED,
        )

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY)

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(fields=[
            MetadataField(value="analytics", title="Analytics Schema"),
        ])
```

## Typed Contracts

Every handler method takes a single typed `Input` and returns a single typed `Output`. The contracts are defined in `application_sdk.handler.contracts`:

### AuthInput / AuthOutput

```python
class AuthInput:
    credentials: dict    # credential payload from the platform

class AuthOutput:
    status: AuthStatus   # SUCCESS or FAILED
    message: str = ""    # optional detail message
```

### PreflightInput / PreflightOutput

```python
class PreflightInput:
    payload: dict        # preflight configuration from the platform

class PreflightOutput:
    status: PreflightStatus  # READY or NOT_READY
    checks: list[Check] = [] # individual check results
```

### MetadataInput / MetadataOutput

```python
class MetadataInput:
    metadata_type: str | None = None
    database: str | None = None
    schema: str | None = None

class MetadataOutput:
    fields: list[MetadataField] = []
```

## Context Injection

There is no `load()` method in v3. The service layer injects `self.context` before each handler method call and clears it after. This makes handlers stateless and safe for concurrent requests.

Access infrastructure through `self.context`:

```python
class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        # Secret store
        api_key = await self.context.get_secret("my-api-key")

        # Credential resolution
        cred = await self.context.resolve_credential(input.credential_ref)

        # State store
        state = await self.context.load_state("handler-state")
        ...
```

## Error Handling with HandlerError

Raise `HandlerError` to return a structured HTTP error response:

```python
from application_sdk.handler import Handler, HandlerError

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        api_key = await self.context.get_secret("my-api-key")
        if not api_key:
            raise HandlerError(
                message="API key not configured",
                http_status=400,
            )
        ...
```

`HandlerError` is translated by the server into an HTTP response with the specified status code and a JSON body containing the error message.

## SQL Handler Pattern

For SQL-based connectors, your handler typically delegates to a SQL client:

```python
class MySQLHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        cred = await self.context.resolve_credential(input.credential_ref)
        try:
            async with create_connection(cred) as conn:
                await conn.execute("SELECT 1")
            return AuthOutput(status=AuthStatus.SUCCESS)
        except Exception:
            return AuthOutput(
                status=AuthStatus.FAILED, message="Connection failed"
            )

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        cred = await self.context.resolve_credential(input.credential_ref)
        async with create_connection(cred) as conn:
            if input.metadata_type == "database":
                rows = await conn.execute(
                    "SELECT database_name FROM databases"
                )
                return MetadataOutput(fields=[
                    MetadataField(
                        value=r["database_name"],
                        title=r["database_name"],
                    )
                    for r in rows
                ])
        return MetadataOutput(fields=[])
```

## Testing Handlers

Test handlers by injecting mock infrastructure:

```python
import pytest
from application_sdk.testing import MockSecretStore
from application_sdk.infrastructure import InfrastructureContext, set_infrastructure
from application_sdk.handler.contracts import AuthInput, AuthStatus

@pytest.fixture
def infra():
    ctx = InfrastructureContext(
        secret_store=MockSecretStore({"my-api-key": "test-secret"}),
    )
    set_infrastructure(ctx)
    return ctx

async def test_auth_success(infra):
    handler = MyHandler()
    result = await handler.test_auth(AuthInput(credentials={}))
    assert result.status == AuthStatus.SUCCESS
```
