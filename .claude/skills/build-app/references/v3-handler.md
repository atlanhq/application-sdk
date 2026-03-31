# V3 Handler — Quick Reference

## What is a Handler?

The Handler provides the HTTP API for your app. It handles three operations:
1. **test_auth** — Validate credentials against the target system
2. **preflight_check** — Run readiness checks (connectivity, permissions)
3. **fetch_metadata** — Discover browsable objects for the Atlan UI

## Handler Contract Types

All from `application_sdk.handler.contracts`:

```python
# Auth
AuthInput(credentials: list[HandlerCredential], connection_id: str, timeout_seconds: int)
AuthOutput(status: AuthStatus, message: str, identities: list[str], scopes: list[str])
AuthStatus.SUCCESS | AuthStatus.FAILED | AuthStatus.EXPIRED | AuthStatus.INVALID_CREDENTIALS

# Preflight
PreflightInput(credentials: list[HandlerCredential], connection_config: dict, checks_to_run: list[str])
PreflightOutput(status: PreflightStatus, checks: list[PreflightCheck], message: str)
PreflightStatus.READY | PreflightStatus.NOT_READY | PreflightStatus.PARTIAL
PreflightCheck(name: str, passed: bool, message: str, duration_ms: float)

# Metadata
MetadataInput(credentials: list[HandlerCredential], connection_config: dict, object_filter: str, max_objects: int)
MetadataOutput(objects: list[MetadataObject], total_count: int, truncated: bool)
MetadataObject(name: str, object_type: str, schema: str, database: str, fields: list[MetadataField])
MetadataField(name: str, field_type: str, nullable: bool, description: str)

# Credential unit
HandlerCredential(key: str, value: str)
```

## Implementation Pattern

```python
from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    HandlerCredential,
    MetadataInput, MetadataOutput, MetadataObject,
    PreflightCheck, PreflightInput, PreflightOutput, PreflightStatus,
)


def _credentials_to_dict(credentials: list[HandlerCredential]) -> dict[str, str]:
    """Convert flat credential list to a lookup dict."""
    return {cred.key: cred.value for cred in credentials}


class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        creds = _credentials_to_dict(input.credentials)
        host = creds.get("host", "")
        api_key = creds.get("api_key", "")

        try:
            client = MyClient(host=host, api_key=api_key)
            await client.ping()
            return AuthOutput(status=AuthStatus.SUCCESS, message="Connected")
        except ConnectionError as e:
            return AuthOutput(status=AuthStatus.FAILED, message=str(e))

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        creds = _credentials_to_dict(input.credentials)
        checks: list[PreflightCheck] = []

        # Check connectivity
        try:
            client = MyClient(host=creds["host"], api_key=creds["api_key"])
            await client.ping()
            checks.append(PreflightCheck(name="connectivity", passed=True))
        except Exception as e:
            checks.append(PreflightCheck(name="connectivity", passed=False, message=str(e)))

        all_passed = all(c.passed for c in checks)
        return PreflightOutput(
            status=PreflightStatus.READY if all_passed else PreflightStatus.NOT_READY,
            checks=checks,
        )

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        creds = _credentials_to_dict(input.credentials)
        client = MyClient(host=creds["host"], api_key=creds["api_key"])
        objects = await client.list_objects()

        return MetadataOutput(
            objects=[
                MetadataObject(name=obj.name, object_type=obj.type)
                for obj in objects[:input.max_objects]
            ],
            total_count=len(objects),
            truncated=len(objects) > input.max_objects,
        )
```

## Rules

1. **No `load()` method.** Context is injected by the framework.
2. **Typed signatures only.** `async def test_auth(self, input: AuthInput) -> AuthOutput`. No `*args`, no `**kwargs`.
3. **Credentials arrive as `list[HandlerCredential]`.** Use `_credentials_to_dict()` helper to convert.
4. **Use `HandlerError` for errors.** `raise HandlerError("message", http_status=400)` for client errors.
5. **Handler is stateless.** Don't store state between calls. Each invocation gets a fresh context.
6. **Access infrastructure via `self.context`** if needed (secrets, state).

## Handler Discovery

The framework discovers the Handler class automatically IF it's in the same module as the App class. If your handler is in a separate file:

**Option A:** Import it in your App module:
```python
# app/my_app.py
from app.handler import MyHandler  # noqa: F401 — registers handler
```

**Option B:** Set the environment variable:
```bash
ATLAN_HANDLER_MODULE=app.handler:MyHandler
```

## DefaultHandler

If you don't need custom handler logic, the SDK provides `DefaultHandler` which always returns SUCCESS/READY/empty. You can also subclass it to override just one method:

```python
from application_sdk.handler.base import DefaultHandler

class MyHandler(DefaultHandler):
    # Only override test_auth, use defaults for the rest
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        # Custom auth logic
        ...
```

## HTTP Endpoints

When the app is running, the handler serves these endpoints:

| Method | Path | Handler Method |
|--------|------|---------------|
| POST | `/workflows/v1/auth` | `test_auth` |
| POST | `/workflows/v1/check` | `preflight_check` |
| POST | `/workflows/v1/metadata` | `fetch_metadata` |
| POST | `/workflows/v1/start` | Starts a workflow run |
| GET | `/workflows/v1/status/<wf_id>/<run_id>` | Workflow status |
| GET | `/workflows/v1/configmaps` | List config maps |
| GET | `/workflows/v1/manifest` | App manifest |
