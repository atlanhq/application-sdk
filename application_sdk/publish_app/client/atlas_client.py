"""Client for interacting with Atlas API with robust error handling and retries."""

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp.client_exceptions import ClientError

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.publish_app.models.plan import PublishConfig

logger = get_logger(__name__)


class AtlasClientError(Exception):
    """Exception raised for errors in the Atlas client."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        """Initialize the error with details.

        Args:
            message: Error message
            status_code: HTTP status code
            response_body: Response body from the API
        """
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message)


class AtlasClient:
    """Client for interacting with Atlas API with robust error handling and retries."""

    def __init__(self, config: Optional[PublishConfig] = None):
        """Initialize with config including auth details.

        Args:
            config: Configuration for the Atlas client
        """
        # FIXME(inishchith): Add support for non-impersonate (ex: service account)

        if config is None:
            config = PublishConfig()
        self.config = config
        self.auth_token = None
        self.session = None

    async def initialize(self):
        """Initialize session with authentication.

        Raises:
            ValueError: If KEYClOAK_MASTER_PASSWORD is not set
            AtlasClientError: If authentication fails
        """
        # Generate auth token with impersonation
        self.auth_token = await self._generate_auth_token(self.config.username)

        # Set up session with headers
        self.session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self.auth_token}",
                "Content-Type": "application/json",
                "x-atlan-agent": "workflow",
                "User-Agent": "AtlasPublishWorkflow",
            }
        )

        logger.info(f"Initialized Atlas client for user {self.config.username}")

    async def close(self):
        """Close the session."""
        if self.session:
            await self.session.close()

    async def create_update_entities(
        self, entities: List[Dict[str, Any]], options: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Create entities in bulk with retry logic.

        Args:
            entities: List of entity definitions to create
            options: Additional options for the API call

        Returns:
            Response from the API

        Raises:
            AtlasClientError: If the API call fails
        """
        endpoint = "/entity/bulk"
        payload = {"entities": entities}
        if options:
            payload.update(options)

        return await self._make_api_call(endpoint, "POST", payload)

    async def create_update_relationships_only(
        self, entities: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Update only relationship attributes of entities.

        This is used for the second pass of self-referential types.

        Args:
            entities: List of entity definitions with only relationship attributes

        Returns:
            Response from the API

        Raises:
            AtlasClientError: If the API call fails
        """
        return await self.create_update_entities(
            entities,
            {"updateRelationshipAttributes": True, "ignoreOtherAttributes": True},
        )

    async def delete_entities(self, guids: List[str]) -> Dict[str, Any]:
        """Delete entities by GUID.

        Args:
            guids: List of GUIDs to delete

        Returns:
            Response from the API

        Raises:
            AtlasClientError: If the API call fails
        """
        endpoint = "/entity/bulk/uniqueAttribute"

        # FIXME(inishchith): the payload is of type
        # {
        #     "typeName": "string",
        #     "uniqueAttributes": {
        #         "qualifiedName": "string"
        #     }
        # }

        payload = {"guids": guids}

        return await self._make_api_call(endpoint, "DELETE", payload)

    async def _make_api_call(
        self, endpoint: str, method: str, payload: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Make API call with retry logic.

        Args:
            endpoint: API endpoint
            method: HTTP method
            payload: Request payload

        Returns:
            Response from the API

        Raises:
            AtlasClientError: If the API call fails after retries
        """
        if not self.session:
            raise AtlasClientError("Atlas client not initialized")

        max_retries = self.config.retry_config["max_retries"]
        backoff_factor = self.config.retry_config["backoff_factor"]
        retry_status_codes = self.config.retry_config["retry_status_codes"]
        fail_fast_status_codes = self.config.retry_config["fail_fast_status_codes"]
        timeout = self.config.atlas_timeout

        retry_count = 0
        last_error = None

        while retry_count <= max_retries:
            try:
                if retry_count > 0:
                    # Exponential backoff
                    delay = backoff_factor**retry_count
                    logger.info(
                        f"Retrying after {delay:.2f}s (attempt {retry_count}/{max_retries})"
                    )
                    await asyncio.sleep(delay)

                logger.debug(f"Making {method} request to {endpoint}")

                # Get base URL from config
                base_url = os.environ.get("ATLAN_ATLAS_URL")
                # base_url = self.config.atlas_api_config.get("base_url", "https://api.atlan.com")
                url = f"{base_url}{endpoint}"

                # Make the request
                async with self.session.request(
                    method=method, url=url, json=payload, timeout=timeout
                ) as response:
                    response_text = await response.text()
                    # Check for failure
                    if response.status >= 400:
                        # Check if we should fail fast
                        if response.status in fail_fast_status_codes:
                            logger.error(
                                f"Atlas API returned {response.status}, failing fast: {response_text}"
                            )
                            raise AtlasClientError(
                                f"Atlas API returned {response.status}",
                                status_code=response.status,
                                response_body=response_text,
                            )

                        # Check if we should retry
                        if (
                            response.status in retry_status_codes
                            and retry_count < max_retries
                        ):
                            logger.warning(
                                f"Atlas API returned {response.status}, will retry: {response_text}"
                            )
                            retry_count += 1
                            last_error = AtlasClientError(
                                f"Atlas API returned {response.status}",
                                status_code=response.status,
                                response_body=response_text,
                            )
                            continue

                        # Otherwise, fail
                        logger.error(
                            f"Atlas API returned {response.status}: {response_text}"
                        )
                        raise AtlasClientError(
                            f"Atlas API returned {response.status}",
                            status_code=response.status,
                            response_body=response_text,
                        )

                    # Success case
                    try:
                        result = json.loads(response_text) if response_text else {}
                        logger.debug(
                            f"Atlas API returned {response.status} with {len(result)} keys"
                        )
                        return result
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Failed to parse Atlas API response as JSON: {response_text}"
                        )
                        return {"raw_response": response_text}

            except (asyncio.TimeoutError, ClientError) as e:
                logger.warning(f"Network error: {str(e)}")

                if retry_count < max_retries:
                    retry_count += 1
                    last_error = AtlasClientError(f"Network error: {str(e)}")
                    continue
                else:
                    logger.error(f"Failed after {max_retries} retries")
                    raise AtlasClientError(
                        f"Network error after {max_retries} retries: {str(e)}"
                    )

        # If we get here, we've exhausted our retries
        if last_error:
            raise last_error

        # This should never happen
        raise AtlasClientError("Unknown error in request")

    async def _generate_auth_token(self, username: str) -> str:
        """Generate auth token with impersonation.

        Args:
            username: Username to impersonate
            master_password: Master password for authentication

        Returns:
            Auth token

        Raises:
            AtlasClientError: If authentication fails
        """

        # FIXME(inishchith): move the env vars to central config
        # Get auth URL from config
        self.token_url = os.environ.get("KEYCLOAK_TOKEN_URL")

        # Get client credentials from config
        self.client_id = os.environ.get("ARGO_CLIENT_ID")
        self.client_secret = os.environ.get("ARGO_CLIENT_SECRET")

        try:
            # Create a temporary session
            async with aiohttp.ClientSession() as session:
                # First call to get initial access token
                async with session.post(
                    self.token_url,
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "client_credentials",
                    },
                ) as first_response:
                    if first_response.status != 200:
                        error_text = await first_response.text()
                        raise AtlasClientError(
                            "Failed to get initial access token",
                            status_code=first_response.status,
                            response_body=error_text,
                        )

                    response = await first_response.json()
                    token = response.get("access_token")
                    if not token:
                        raise AtlasClientError("No access token in response")

                    if username == "":
                        # FIXME(inishchith): this is temporary shunt to allow for connection entity creation
                        # review this with the team for the creator
                        return token

                    # Second call to get impersonation token
                    async with session.post(
                        self.token_url,
                        data={
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                            "subject_token": token,
                            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                            "requested_subject": username,
                        },
                    ) as second_response:
                        if second_response.status != 200:
                            error_text = await second_response.text()
                            raise AtlasClientError(
                                "Failed to get impersonation token",
                                status_code=second_response.status,
                                response_body=error_text,
                            )

                        response = await second_response.json()
                        token = response.get("access_token")
                        if not token:
                            raise AtlasClientError("No impersonation token in response")

                        return token

        except (asyncio.TimeoutError, ClientError) as e:
            raise AtlasClientError(f"Network error during authentication: {str(e)}")
        except json.JSONDecodeError as e:
            raise AtlasClientError(f"Invalid JSON response: {str(e)}")
