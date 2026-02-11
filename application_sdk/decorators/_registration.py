"""Activity registration flush and HTTP client for the automation engine."""

import asyncio
import os
from typing import Any, List, Optional
from urllib.parse import urlparse

import httpx

from application_sdk.decorators._models import (
    APP_QUALIFIED_NAME_PREFIX,
    ENDPOINT_APPS,
    ENDPOINT_SERVER_READY,
    ENDPOINT_TOOLS,
    ENV_AUTOMATION_ENGINE_API_URL,
    TIMEOUT_API_REQUEST,
    TIMEOUT_HEALTH_CHECK,
    ActivitySpec,
)
from application_sdk.decorators._schema import _build_tool_dict
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Retry constants
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0  # seconds
RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})

# URL validation constants
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal"})


# =============================================================================
# URL validation (T3)
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
    """Resolve the automation engine API URL from the argument or environment.

    Priority:
    1. Explicit ``automation_engine_api_url`` argument
    2. ``ATLAN_AUTOMATION_ENGINE_API_URL`` environment variable
    3. Constructed from ``ATLAN_AUTOMATION_ENGINE_API_HOST`` + ``ATLAN_AUTOMATION_ENGINE_API_PORT``
    """
    if automation_engine_api_url:
        return automation_engine_api_url

    env_url = os.environ.get(ENV_AUTOMATION_ENGINE_API_URL)
    if env_url:
        return env_url

    host = os.environ.get("ATLAN_AUTOMATION_ENGINE_API_HOST")
    port = os.environ.get("ATLAN_AUTOMATION_ENGINE_API_PORT")
    if host and port:
        return f"http://{host}:{port}"

    return None


def _resolve_app_qualified_name(
    app_qualified_name: Optional[str], app_name: str
) -> str:
    """Resolve the app qualified name from the argument or compute from app_name."""
    if app_qualified_name:
        return app_qualified_name

    env_qn = os.environ.get("ATLAN_APP_QUALIFIED_NAME")
    if env_qn:
        return env_qn

    # Compute: default/apps/<app_name_with_underscores>
    return f"{APP_QUALIFIED_NAME_PREFIX}{app_name.replace('-', '_').lower()}"


# =============================================================================
# Retry helper (W5)
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


async def _flush_specs(
    specs_to_register: List[ActivitySpec],
    app_name: str,
    workflow_task_queue: str,
    automation_engine_api_url: Optional[str] = None,
    app_qualified_name: Optional[str] = None,
    max_retries: int = MAX_RETRIES,
) -> None:
    """Push a list of activity specs to the automation engine HTTP API.

    This is the internal implementation called by the public
    ``flush_activity_registrations`` after it snapshots and clears the
    global ``ACTIVITY_SPECS``.
    """
    if not specs_to_register:
        logger.info("No activities to register")
        return

    base_url = _resolve_automation_engine_api_url(automation_engine_api_url)
    if not base_url:
        logger.warning(
            "Automation engine API URL not configured. "
            "Set ATLAN_AUTOMATION_ENGINE_API_URL or pass automation_engine_api_url. "
            "Skipping activity registration."
        )
        return

    # T3: Validate URL before making any requests
    try:
        _validate_base_url(base_url)
    except ValueError as e:
        logger.warning(
            f"Invalid automation engine URL: {e}. Skipping activity registration."
        )
        return

    qualified_name = _resolve_app_qualified_name(app_qualified_name, app_name)

    # W3 / T5: Single httpx session for all requests
    async with httpx.AsyncClient(timeout=TIMEOUT_API_REQUEST) as client:
        # Health check
        try:
            await _request_with_retry(
                client,
                "GET",
                f"{base_url}{ENDPOINT_SERVER_READY}",
                max_retries=max_retries,
                timeout=TIMEOUT_HEALTH_CHECK,
            )
            logger.info("Automation engine health check passed")
        except Exception as e:
            logger.warning(
                f"Automation engine health check failed: {e}. "
                "Skipping activity registration."
            )
            return

        logger.info(
            f"Registering {len(specs_to_register)} activities with automation engine"
        )

        # Upsert app
        try:
            await _request_with_retry(
                client,
                "POST",
                f"{base_url}{ENDPOINT_APPS}",
                max_retries=max_retries,
                json={
                    "name": app_name,
                    "task_queue": workflow_task_queue,
                    "qualified_name": qualified_name,
                },
            )
            logger.info(f"Successfully upserted app '{app_name}'")
        except Exception as e:
            # W4: Short-circuit — don't attempt tool registration if upsert failed
            logger.warning(
                f"Failed to upsert app '{app_name}': {e}. "
                "Skipping tool registration."
            )
            return

        await asyncio.sleep(5)

        # Build and send tools
        tools = [_build_tool_dict(item) for item in specs_to_register]

        try:
            await _request_with_retry(
                client,
                "POST",
                f"{base_url}{ENDPOINT_TOOLS}",
                max_retries=max_retries,
                json={
                    "app_qualified_name": qualified_name,
                    "app_name": app_name,
                    "task_queue": workflow_task_queue,
                    "tools": tools,
                },
            )
            logger.info(
                f"Successfully registered {len(tools)} activities "
                "with automation engine"
            )
        except Exception as e:
            logger.warning(
                f"Failed to register activities with automation engine: {e}"
            )
