# Handlers

Handlers implement the API contract for your application's HTTP endpoints: authentication testing, preflight checks, and metadata browsing. In v3, handlers use the `Handler` ABC with typed contracts and automatic context injection, replacing v2's `HandlerInterface` with its untyped `*args/**kwargs` signatures and manual `load()` method.

## Defining a Handler

```python
from application_sdk.handler import Handler
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    PreflightInput, PreflightOutput, PreflightStatus, PreflightCheck,
    MetadataInput, SqlMetadataOutput, SqlMetadataObject,
)

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        api_key = self.context.get_credential("api_key")
        ok = await verify_key(api_key)
        return AuthOutput(
            status=AuthStatus.SUCCESS if ok else AuthStatus.FAILED,
        )

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY)

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[
            SqlMetadataObject(TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="ANALYTICS"),
        ])
```

## Typed Contracts

Every handler method takes a single typed `Input` and returns a single typed `Output`. The contracts are defined in `application_sdk.handler.contracts`:

### AuthInput / AuthOutput

```python
class AuthInput:
    credentials: list[HandlerCredential] = []  # credential key/value pairs
    connection_id: str = ""                     # optional connection ID
    timeout_seconds: int = 30                   # max wait time

class AuthOutput:
    status: AuthStatus       # SUCCESS, FAILED, EXPIRED, or INVALID_CREDENTIALS
    message: str = ""        # optional detail message
    identities: list[str] = []  # verified identities (usernames, roles)
    scopes: list[str] = []     # authorized scopes or permissions
    expires_at: str = ""       # ISO-8601 expiry timestamp
```

Each `HandlerCredential` has a `key: str` and `value: str`.

### PreflightInput / PreflightOutput

```python
class PreflightInput:
    credentials: list[HandlerCredential] = []  # credentials for preflight
    connection_config: dict[str, Any] = {}     # host, port, database, etc.
    checks_to_run: list[str] = []              # specific checks (empty = all)
    timeout_seconds: int = 60                  # max wait time

class PreflightOutput:
    status: PreflightStatus           # READY, NOT_READY, or PARTIAL
    checks: list[PreflightCheck] = [] # individual check results
    message: str = ""                 # human-readable summary
    total_duration_ms: float = 0.0    # total time for all checks
```

### MetadataInput / MetadataOutput

```python
class MetadataInput:
    credentials: list[HandlerCredential] = []  # credentials for discovery
    connection_config: dict[str, Any] = {}     # connection configuration
    object_filter: str = ""                    # filter pattern (e.g. 'public.*')
    include_fields: bool = True                # include field/column details
    max_objects: int = 1000                    # max objects to return
    timeout_seconds: int = 120                 # max wait time

class MetadataOutput:
    objects: list[Any] = []  # base class — use SqlMetadataOutput or ApiMetadataOutput

class SqlMetadataOutput(MetadataOutput):
    objects: list[SqlMetadataObject] = []  # for sqltree widget

class ApiMetadataOutput(MetadataOutput):
    objects: list[ApiMetadataObject] = []  # for apitree widget
```

## Context Injection

There is no `load()` method in v3. The service layer injects `self.context` before each handler method call and clears it after. This makes handlers stateless and safe for concurrent requests.

Access infrastructure through `self.context`:

```python
class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        # Get a credential value by key from the request credentials
        api_key = self.context.get_credential("api_key")

        # Get a secret from the secret store
        secret = await self.context.get_secret("my-secret-name")

        # Access all credentials as a list
        all_creds = self.context.credentials

        # Check if a credential exists
        if self.context.has_credential("api_key"):
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
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    MetadataInput, SqlMetadataOutput, SqlMetadataObject,
)

class MySQLHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        host = self.context.get_credential("host")
        username = self.context.get_credential("username")
        password = self.context.get_credential("password")
        try:
            async with create_connection(host, username, password) as conn:
                await conn.execute("SELECT 1")
            return AuthOutput(status=AuthStatus.SUCCESS)
        except Exception:
            return AuthOutput(
                status=AuthStatus.FAILED, message="Connection failed"
            )

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        host = self.context.get_credential("host")
        username = self.context.get_credential("username")
        password = self.context.get_credential("password")
        async with create_connection(host, username, password) as conn:
            rows = await conn.execute(
                "SELECT TABLE_CATALOG, TABLE_SCHEMA "
                "FROM information_schema.schemata"
            )
            return SqlMetadataOutput(objects=[
                SqlMetadataObject(
                    TABLE_CATALOG=r["TABLE_CATALOG"],
                    TABLE_SCHEMA=r["TABLE_SCHEMA"],
                )
                for r in rows
            ])
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
    result = await handler.test_auth(AuthInput(credentials=[]))
    assert result.status == AuthStatus.SUCCESS
```
