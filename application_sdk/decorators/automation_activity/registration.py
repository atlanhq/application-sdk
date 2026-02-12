"""Activity registration flush and HTTP client for the automation engine."""

import asyncio
from typing import Any, List, Optional
from urllib.parse import urlparse

import httpx

from application_sdk.constants import (
    APP_QUALIFIED_NAME,
    APP_UPSERT_PROPAGATION_DELAY,
    AUTOMATION_ENGINE_API_HOST,
    AUTOMATION_ENGINE_API_PORT,
    AUTOMATION_ENGINE_API_URL,
)
from application_sdk.decorators.automation_activity.models import (
    APP_QUALIFIED_NAME_PREFIX,
    ENDPOINT_APPS,
    ENDPOINT_SERVER_READY,
    ENDPOINT_TOOLS,
    TIMEOUT_API_REQUEST,
    TIMEOUT_HEALTH_CHECK,
    ActivitySpec,
    AppSpec,
    ToolRegistrationRequest,
)
from application_sdk.decorators.automation_activity.schema import _build_tool_spec
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Retry constants
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0  # seconds
RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})

# Default delay between app upsert and tool registration to allow the automation
# engine to propagate the app record before accepting tool registrations.
# Configurable via ATLAN_APP_UPSERT_PROPAGATION_DELAY env var.
DEFAULT_APP_UPSERT_PROPAGATION_DELAY = 5.0  # seconds

# URL validation constants
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal"})


# =============================================================================
# URL validation
# =============================================================================


def _validate_base_url(url: str) -> None:
    """Validate that the automation engine URL is safe to call.

    Blocks non-HTTP(S) schemes and known cloud metadata endpoints
    to prevent SSRF attacks via environment variable injection.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"Unsupported URL scheme '{parsed.scheme}' — only http/https allowed"
        )
    if not parsed.hostname:
        raise ValueError(f"Invalid URL (no hostname): {url}")
    if parsed.hostname in _BLOCKED_HOSTS:
        raise ValueError(f"Blocked host: {parsed.hostname}")


# =============================================================================
# URL resolution
# =============================================================================


def _resolve_automation_engine_api_url(
    automation_engine_api_url: Optional[str],
) -> Optional[str]:
    """Resolve the automation engine API URL from argument or environment."""
    candidates = [
        automation_engine_api_url,
        AUTOMATION_ENGINE_API_URL,
        f"http://{AUTOMATION_ENGINE_API_HOST}:{AUTOMATION_ENGINE_API_PORT}"
        if AUTOMATION_ENGINE_API_HOST and AUTOMATION_ENGINE_API_PORT
        else None,
    ]
    return next((c for c in candidates if c), None)


def _resolve_app_qualified_name(
    app_qualified_name: Optional[str], app_name: str
) -> str:
    """Resolve the app qualified name from argument, env, or compute from app_name."""
    return (
        app_qualified_name
        or APP_QUALIFIED_NAME
        or f"{APP_QUALIFIED_NAME_PREFIX}{app_name.replace('-', '_').lower()}"
    )


# =============================================================================
# Retry helper
# =============================================================================


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    max_retries: int = MAX_RETRIES,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an HTTP request with retry on transient failures.

    Retries on connection errors, timeouts, and retryable HTTP status codes
    (429, 502, 503, 504) using exponential backoff.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
                delay = RETRY_BACKOFF_BASE * (2**attempt)
                logger.info(
                    f"Retryable status {response.status_code} from {url}, "
                    f"retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError:
            raise
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            last_exc = e
            if attempt < max_retries:
                delay = RETRY_BACKOFF_BASE * (2**attempt)
                logger.info(
                    f"Request to {url} failed: {e}, "
                    f"retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
            else:
                raise
    # Unreachable, but satisfies type checker
    raise last_exc  # type: ignore[misc]


# =============================================================================
# Internal flush implementation
# =============================================================================


async def _check_engine_health(
    client: httpx.AsyncClient, base_url: str, max_retries: int
) -> bool:
    """Return True if automation engine is healthy."""
    try:
        await _request_with_retry(
            client,
            "GET",
            f"{base_url}{ENDPOINT_SERVER_READY}",
            max_retries=max_retries,
            timeout=TIMEOUT_HEALTH_CHECK,
        )
        logger.info("Automation engine health check passed")
        return True
    except (
        httpx.HTTPStatusError,
        httpx.ConnectError,
        httpx.TimeoutException,
    ) as e:
        logger.warning(
            f"Automation engine health check failed: {e}. "
            "Skipping activity registration."
        )
        return False


async def _upsert_app(
    client: httpx.AsyncClient,
    base_url: str,
    app_spec: AppSpec,
    max_retries: int,
) -> bool:
    """Upsert app record. Return True on success."""
    try:
        await _request_with_retry(
            client,
            "POST",
            f"{base_url}{ENDPOINT_APPS}",
            max_retries=max_retries,
            json=app_spec.model_dump(exclude_none=True),
        )
        logger.info(f"Successfully upserted app '{app_spec.name}'")
        return True
    except (
        httpx.HTTPStatusError,
        httpx.ConnectError,
        httpx.TimeoutException,
    ) as e:
        logger.warning(
            f"Failed to upsert app '{app_spec.name}': {e}. "
            "Skipping tool registration."
        )
        return False


async def _register_tools(
    client: httpx.AsyncClient,
    base_url: str,
    registration_request: ToolRegistrationRequest,
    max_retries: int,
) -> bool:
    """Register tools with automation engine. Return True on success."""
    try:
        await _request_with_retry(
            client,
            "POST",
            f"{base_url}{ENDPOINT_TOOLS}",
            max_retries=max_retries,
            json=registration_request.model_dump(exclude_none=True),
        )
        logger.info(
            f"Successfully registered {len(registration_request.tools)} activities "
            "with automation engine"
        )
        return True
    except (
        httpx.HTTPStatusError,
        httpx.ConnectError,
        httpx.TimeoutException,
    ) as e:
        logger.warning(f"Failed to register activities with automation engine: {e}")
        return False


async def _flush_specs(
    specs_to_register: List[ActivitySpec],
    app_name: str,
    workflow_task_queue: str,
    automation_engine_api_url: Optional[str] = None,
    app_qualified_name: Optional[str] = None,
    max_retries: int = MAX_RETRIES,
) -> bool:
    """Push a list of activity specs to the automation engine HTTP API.

    This is the internal implementation called by the public
    ``flush_activity_registrations`` after it snapshots and clears the
    global ``ACTIVITY_SPECS``.

    Returns:
        ``True`` if all registrations succeeded, ``False`` otherwise.
    """
    if not specs_to_register:
        logger.info("No activities to register")
        return True

    base_url = _resolve_automation_engine_api_url(automation_engine_api_url)
    if not base_url:
        logger.warning(
            "Automation engine API URL not configured. "
            "Set ATLAN_AUTOMATION_ENGINE_API_URL or pass automation_engine_api_url. "
            "Skipping activity registration."
        )
        return False

    # Validate URL before making any requests
    try:
        _validate_base_url(base_url)
    except ValueError as e:
        logger.warning(
            f"Invalid automation engine URL: {e}. Skipping activity registration."
        )
        return False

    qualified_name = _resolve_app_qualified_name(app_qualified_name, app_name)

    logger.info(
        f"Registering {len(specs_to_register)} activities with automation engine"
    )

    app_spec = AppSpec(
        name=app_name,
        task_queue=workflow_task_queue,
        qualified_name=qualified_name,
    )
    tool_specs = [_build_tool_spec(item) for item in specs_to_register]
    registration_request = ToolRegistrationRequest(
        app_qualified_name=qualified_name,
        app_name=app_name,
        task_queue=workflow_task_queue,
        tools=tool_specs,
    )

    # TODO: Replace this sleep with a readiness-polling loop or
    # server-side confirmation that the app record is propagated.
    propagation_delay = (
        float(APP_UPSERT_PROPAGATION_DELAY)
        if APP_UPSERT_PROPAGATION_DELAY
        else DEFAULT_APP_UPSERT_PROPAGATION_DELAY
    )

    async with httpx.AsyncClient(timeout=TIMEOUT_API_REQUEST) as client:
        if not await _check_engine_health(client, base_url, max_retries):
            return False
        if not await _upsert_app(client, base_url, app_spec, max_retries):
            return False
        await asyncio.sleep(propagation_delay)
        return await _register_tools(
            client, base_url, registration_request, max_retries
        )
