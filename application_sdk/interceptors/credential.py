"""Credential interceptor for Temporal workflows.

Handles credential bootstrap at workflow start and cleanup on completion.
Credentials are resolved via Heracles JWT and fetched from Dapr secret store.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Type

from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.converter import default as default_converter
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.credentials.context import (
    create_credential_context,
    set_credential_context,
)
from application_sdk.credentials.exceptions import CredentialBootstrapError
from application_sdk.credentials.handle import CredentialHandle
from application_sdk.credentials.http_client import AuthenticatedHTTPClient
from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.resolver import CredentialResolver
from application_sdk.credentials.store import WorkerCredentialStore
from application_sdk.credentials.types import Credential
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.credentials.heracles_client import SlotMapping

logger = get_logger(__name__)

# Header keys for credential JWT
CREDENTIAL_JWT_HEADER = "x-credential-jwt"
CREDENTIAL_REFRESH_TOKEN_HEADER = "x-credential-refresh-token"


class CredentialWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Workflow interceptor that bootstraps credentials at workflow start.

    Flow:
    1. Extract JWT from Temporal headers (x-credential-jwt)
    2. Call Heracles to resolve credential mapping (slot -> guid + field mappings)
    3. Fetch secrets from Dapr secret store using credential GUIDs
    4. Apply field mappings to transform field names
    5. Create CredentialHandles and HTTP clients
    6. Cleanup on workflow completion/failure

    Dapr abstracts the actual secret backend (Vault, AWS Secrets Manager,
    customer environment, external secret managers, etc.)
    """

    def __init__(
        self,
        next: WorkflowInboundInterceptor,
        credential_declarations: Optional[List[Credential]] = None,
    ) -> None:
        """Initialize the interceptor.

        Args:
            next: Next interceptor in the chain.
            credential_declarations: Optional list of credential declarations.
        """
        super().__init__(next)
        self._credential_declarations = credential_declarations or []
        self._store = WorkerCredentialStore.get_instance()

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Execute workflow with credential bootstrap.

        Bootstrap credentials at workflow start and clean up on completion.
        """
        workflow_id = workflow.info().workflow_id

        try:
            await self._bootstrap_credentials(workflow_id, input)
            return await super().execute_workflow(input)

        finally:
            # Clean up secrets from secret store (local JSON file or production store)
            await self._cleanup_secrets(workflow_id)

            # Clean up in-memory credential data
            self._store.cleanup(workflow_id)
            logger.debug(
                f"Cleaned up credentials for workflow {workflow_id}",
                extra={"workflow_id": workflow_id},
            )

    async def _cleanup_secrets(self, workflow_id: str) -> None:
        """Clean up workflow-scoped secrets from the secret store.

        In local mode, deletes credentials from the JSON secrets file.
        In production, secrets may be managed externally and not deleted.

        Args:
            workflow_id: The workflow ID.
        """
        from application_sdk.constants import DEPLOYMENT_NAME, LOCAL_ENVIRONMENT
        from application_sdk.services.secretstore import SecretStore

        if DEPLOYMENT_NAME != LOCAL_ENVIRONMENT:
            # Production: secrets are managed externally
            return

        # Get credential mapping to find GUIDs to delete
        credential_mapping = self._store.get_credential_mapping(workflow_id)
        for slot_name, credential_guid in credential_mapping.items():
            try:
                SecretStore.delete_secret(credential_guid)
                logger.debug(
                    f"Deleted secret for slot '{slot_name}' (guid: {credential_guid})",
                    extra={"workflow_id": workflow_id, "slot_name": slot_name},
                )
            except Exception as e:
                logger.warning(
                    f"Failed to delete secret for slot '{slot_name}': {e}",
                    extra={"workflow_id": workflow_id, "slot_name": slot_name},
                )

    async def _bootstrap_credentials(
        self,
        workflow_id: str,
        input: ExecuteWorkflowInput,
    ) -> None:
        """Bootstrap credentials from JWT (production) or workflow config (local).

        In production mode:
            - JWT is extracted from Temporal headers
            - Heracles resolves credential mapping
            - Secrets are fetched from Dapr

        In local mode (DEPLOYMENT_NAME == "local"):
            - credential_mapping is read from workflow config
            - Secrets are fetched from local StateStore
            - No JWT/Heracles required

        Args:
            workflow_id: The workflow ID.
            input: Workflow execution input.
        """
        from application_sdk.constants import DEPLOYMENT_NAME, LOCAL_ENVIRONMENT

        # Check for local mode first
        if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
            await self._bootstrap_credentials_local(workflow_id, input)
            return

        # Production mode: Extract JWT from headers
        jwt_token = self._extract_header(input.headers, CREDENTIAL_JWT_HEADER)
        refresh_token = self._extract_header(
            input.headers, CREDENTIAL_REFRESH_TOKEN_HEADER
        )

        if not jwt_token:
            logger.debug(f"No credential JWT in headers for workflow {workflow_id}")
            return

        logger.debug(
            f"Bootstrapping credentials for workflow {workflow_id}",
            extra={"workflow_id": workflow_id},
        )

        # Store refresh token if provided (for long-running workflows)
        if refresh_token:
            self._store.set_refresh_token(workflow_id, refresh_token)

        # Resolve credential mapping via Heracles
        from application_sdk.credentials.heracles_client import HeraclesClient

        async with HeraclesClient() as heracles:
            mapping_response = await heracles.resolve_credential_mapping(jwt_token)

        if not mapping_response.slot_mappings:
            logger.debug(
                f"Empty credential mapping returned for workflow {workflow_id}"
            )
            return

        # Store the simple mapping (slot -> guid) for reference
        simple_mapping = {
            slot.slot_name: slot.credential_guid
            for slot in mapping_response.slot_mappings
        }
        self._store.set_credential_mapping(workflow_id, simple_mapping)

        # Bootstrap each slot
        for slot_mapping in mapping_response.slot_mappings:
            try:
                await self._bootstrap_slot(workflow_id, slot_mapping)
            except Exception as e:
                logger.error(
                    f"Failed to bootstrap credential for slot '{slot_mapping.slot_name}': {e}",
                    extra={
                        "workflow_id": workflow_id,
                        "slot_name": slot_mapping.slot_name,
                        "credential_guid": slot_mapping.credential_guid,
                    },
                )
                raise CredentialBootstrapError(
                    f"Failed to bootstrap credential for slot '{slot_mapping.slot_name}': {e}"
                ) from e

        # Mark as initialized
        self._store.mark_initialized(workflow_id)

        # JWT is discarded after bootstrap (security measure)
        logger.debug(
            f"Credential bootstrap complete for workflow {workflow_id}",
            extra={
                "workflow_id": workflow_id,
                "slots": [s.slot_name for s in mapping_response.slot_mappings],
            },
        )

    async def _bootstrap_credentials_local(
        self,
        workflow_id: str,
        input: ExecuteWorkflowInput,
    ) -> None:
        """Bootstrap credentials in local mode from workflow config.

        In local mode, credentials are stored in a local JSON file that serves
        as a Dapr-compatible secret store. The credential mapping is passed
        in the workflow config or stored in StateStore with the workflow config.

        The read path is unified: credentials are always fetched via
        SecretStore.get_secret(), which reads from the JSON file in local mode.

        Expected workflow config structure:
        {
            "credential_mapping": {
                "slot_name": "credential_guid",
                ...
            }
        }

        Args:
            workflow_id: The workflow ID.
            input: Workflow execution input.
        """
        # Extract workflow config from args
        workflow_config: Dict[str, Any] = {}

        if input.args:
            workflow_config = input.args[0] if input.args else {}
            if not isinstance(workflow_config, dict):
                logger.warning(
                    f"Workflow args[0] is not a dict for workflow {workflow_id}. "
                    f"Expected dict with credential_mapping, got {type(workflow_config).__name__}. "
                    f"Skipping credential bootstrap.",
                    extra={
                        "workflow_id": workflow_id,
                        "args_type": type(workflow_config).__name__,
                    },
                )
                return

        credential_mapping = workflow_config.get("credential_mapping", {})

        # If credential_mapping not in args, try to fetch from StateStore
        # This handles the case where workflow is started with just workflow_id
        # and the full config (including credential_mapping) is in StateStore
        if not credential_mapping and workflow_id:
            try:
                from application_sdk.services.statestore import StateStore, StateType

                stored_config = await StateStore.get_state(
                    workflow_id, StateType.WORKFLOWS
                )
                if stored_config and isinstance(stored_config, dict):
                    credential_mapping = stored_config.get("credential_mapping", {})

                    # Also check for credential_guid (single credential case)
                    # and create credential_mapping from it using first declared slot
                    if not credential_mapping and stored_config.get("credential_guid"):
                        credential_guid = stored_config["credential_guid"]
                        # Use first declared credential slot name, or "default"
                        slot_name = "default"
                        if self._credential_declarations:
                            slot_name = self._credential_declarations[0].name
                        credential_mapping = {slot_name: credential_guid}
                        logger.debug(
                            f"Created credential_mapping from credential_guid: {slot_name} -> {credential_guid}",
                            extra={"workflow_id": workflow_id},
                        )

                    if credential_mapping:
                        logger.debug(
                            f"Fetched credential_mapping from StateStore for {workflow_id}",
                            extra={
                                "workflow_id": workflow_id,
                                "slots": list(credential_mapping.keys()),
                            },
                        )
            except Exception as e:
                logger.debug(
                    f"Could not fetch workflow config from StateStore: {e}",
                    extra={"workflow_id": workflow_id},
                )
        if not credential_mapping:
            logger.debug(
                f"No credential_mapping in workflow config for {workflow_id}",
                extra={"workflow_id": workflow_id},
            )
            return

        logger.debug(
            f"Bootstrapping credentials (local mode) for workflow {workflow_id}",
            extra={
                "workflow_id": workflow_id,
                "slots": list(credential_mapping.keys()),
            },
        )

        # Store the mapping
        self._store.set_credential_mapping(workflow_id, credential_mapping)

        # Bootstrap each slot
        from application_sdk.services.secretstore import SecretStore

        for slot_name, credential_guid in credential_mapping.items():
            try:
                # Fetch credentials from local secrets file via unified get_secret() path
                # This reads from Dapr (local.file secretstore → JSON file)
                credentials = SecretStore.get_secret(credential_guid)
                if not credentials:
                    logger.warning(
                        f"No credentials found for slot '{slot_name}' with guid '{credential_guid}'",
                        extra={"workflow_id": workflow_id, "slot_name": slot_name},
                    )
                    continue

                # Find declaration and resolve protocol
                declaration = self._find_declaration(slot_name)
                if declaration:
                    protocol = CredentialResolver.resolve(declaration)
                    # Apply default values from declaration for missing fields
                    credentials = self._apply_default_values(credentials, declaration)
                else:
                    # Default to connection protocol if no declaration
                    from application_sdk.credentials.protocols.connection import (
                        ConnectionProtocol,
                    )

                    protocol = ConnectionProtocol()

                # Create handle
                handle = self._create_handle(
                    slot_name=slot_name,
                    credentials=credentials,
                    workflow_id=workflow_id,
                    protocol=protocol,
                )
                self._store.set_handle(workflow_id, slot_name, handle)

                # Store raw credentials for refresh operations
                self._store.set_raw_credentials(workflow_id, slot_name, credentials)

                # Create HTTP client with base URL if configured
                base_url = self._get_base_url(slot_name, credentials)
                timeout = declaration.timeout if declaration else 30.0
                max_retries = declaration.max_retries if declaration else 1

                http_client = AuthenticatedHTTPClient(
                    slot_name=slot_name,
                    credentials=credentials,
                    protocol=protocol,
                    workflow_id=workflow_id,
                    base_url=base_url,
                    timeout=timeout,
                    max_retries=max_retries,
                )
                self._store.set_http_client(workflow_id, slot_name, http_client)

                logger.debug(
                    f"Bootstrapped credential (local mode) for slot '{slot_name}'",
                    extra={
                        "workflow_id": workflow_id,
                        "slot_name": slot_name,
                        "protocol_type": type(protocol).__name__,
                    },
                )

            except Exception as e:
                logger.error(
                    f"Failed to bootstrap credential (local mode) for slot '{slot_name}': {e}",
                    extra={
                        "workflow_id": workflow_id,
                        "slot_name": slot_name,
                        "credential_guid": credential_guid,
                    },
                )
                raise CredentialBootstrapError(
                    f"Failed to bootstrap credential for slot '{slot_name}': {e}"
                ) from e

        # Mark as initialized
        self._store.mark_initialized(workflow_id)

        logger.debug(
            f"Credential bootstrap (local mode) complete for workflow {workflow_id}",
            extra={
                "workflow_id": workflow_id,
                "slots": list(credential_mapping.keys()),
            },
        )

    async def _bootstrap_slot(
        self,
        workflow_id: str,
        slot_mapping: "SlotMapping",
    ) -> None:
        """Bootstrap a single credential slot.

        Args:
            workflow_id: The workflow ID.
            slot_mapping: Slot mapping with credential GUID and field mappings.
        """
        from application_sdk.credentials.heracles_client import apply_field_mappings
        from application_sdk.services.secretstore import SecretStore

        slot_name = slot_mapping.slot_name
        credential_guid = slot_mapping.credential_guid
        field_mappings = slot_mapping.field_mappings

        # Fetch credentials from Dapr secret store
        # SecretStore.get_secret() abstracts the backend (Vault, AWS SM, etc.)
        raw_credentials = SecretStore.get_secret(credential_guid)

        # Apply field mappings to transform field names
        # e.g., {"db_password": "password"} transforms db_password -> password
        credentials = apply_field_mappings(raw_credentials, field_mappings)

        # Find the credential declaration for this slot
        declaration = self._find_declaration(slot_name)
        if declaration:
            protocol = CredentialResolver.resolve(declaration)
        else:
            # Default to a pass-through protocol if no declaration
            from application_sdk.credentials.protocols.connection import (
                ConnectionProtocol,
            )

            protocol = ConnectionProtocol()

        # Create handle
        handle = self._create_handle(
            slot_name=slot_name,
            credentials=credentials,
            workflow_id=workflow_id,
            protocol=protocol,
        )
        self._store.set_handle(workflow_id, slot_name, handle)

        # Store raw credentials for refresh operations
        self._store.set_raw_credentials(workflow_id, slot_name, credentials)

        # Create HTTP client with base URL if configured
        # Note: declaration was already fetched above
        base_url = self._get_base_url(slot_name, credentials)
        timeout = declaration.timeout if declaration else 30.0
        max_retries = declaration.max_retries if declaration else 1

        http_client = AuthenticatedHTTPClient(
            slot_name=slot_name,
            credentials=credentials,
            protocol=protocol,
            workflow_id=workflow_id,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._store.set_http_client(workflow_id, slot_name, http_client)

        logger.debug(
            f"Bootstrapped credential for slot '{slot_name}'",
            extra={
                "workflow_id": workflow_id,
                "slot_name": slot_name,
                "protocol_type": type(protocol).__name__,
                "has_field_mappings": bool(field_mappings),
            },
        )

    def _extract_header(
        self,
        headers: Mapping[str, Payload],
        key: str,
    ) -> Optional[str]:
        """Extract a string value from Temporal headers.

        Args:
            headers: Temporal headers.
            key: Header key to extract.

        Returns:
            Header value or None.
        """
        if key not in headers:
            return None

        try:
            payload_converter = default_converter().payload_converter
            return payload_converter.from_payload(headers[key], type_hint=str)
        except Exception as e:
            logger.warning(f"Failed to extract header '{key}': {e}")
            return None

    def _find_declaration(self, slot_name: str) -> Optional[Credential]:
        """Find credential declaration by slot name.

        Args:
            slot_name: Name of the slot.

        Returns:
            Credential declaration or None.
        """
        for decl in self._credential_declarations:
            if decl.name == slot_name:
                return decl
        return None

    def _apply_default_values(
        self,
        credentials: Dict[str, Any],
        declaration: Credential,
    ) -> Dict[str, Any]:
        """Apply default values from credential declaration for missing fields.

        When credentials are submitted (e.g., from Credential-v2 widget), some
        fields may be omitted if they have default values in the declaration.
        This method fills in those defaults.

        Args:
            credentials: Credentials from secret store.
            declaration: Credential declaration with field specs.

        Returns:
            Credentials with default values applied for missing fields.
        """
        if not declaration.fields:
            return credentials

        result = dict(credentials)

        for field_name, field_spec in declaration.fields.items():
            # Only apply default if field is missing and has a default value
            if field_name not in result and field_spec.default_value is not None:
                result[field_name] = field_spec.default_value
                logger.debug(
                    f"Applied default value for field '{field_name}'",
                    extra={
                        "slot_name": declaration.name,
                        "field_name": field_name,
                    },
                )

        return result

    def _get_base_url(
        self, slot_name: str, credentials: Dict[str, Any]
    ) -> Optional[str]:
        """Get the base URL for a credential slot.

        The base URL can be configured in two ways:
        1. Static: Defined in the credential declaration (base_url)
        2. Dynamic: Read from a credential field (base_url_field)

        Args:
            slot_name: Name of the slot.
            credentials: Credential values.

        Returns:
            Base URL or None if not configured.
        """
        declaration = self._find_declaration(slot_name)
        if not declaration:
            return None

        # Static base URL takes precedence
        if declaration.base_url:
            return declaration.base_url

        # Dynamic base URL from credential field
        if declaration.base_url_field:
            base_url = credentials.get(declaration.base_url_field)
            if base_url:
                # Ensure URL doesn't have trailing slash
                return base_url.rstrip("/")
            else:
                logger.warning(
                    f"base_url_field '{declaration.base_url_field}' not found in credentials "
                    f"for slot '{slot_name}'",
                    extra={
                        "slot_name": slot_name,
                        "base_url_field": declaration.base_url_field,
                    },
                )

        return None

    def _create_handle(
        self,
        slot_name: str,
        credentials: Dict[str, Any],
        workflow_id: str,
        protocol: BaseProtocol,
    ) -> CredentialHandle:
        """Create a credential handle.

        Args:
            slot_name: Name of the slot.
            credentials: Credential values (after field mapping applied).
            workflow_id: The workflow ID.
            protocol: Protocol instance.

        Returns:
            CredentialHandle instance.
        """
        # Materialize credentials via protocol
        result = protocol.materialize(credentials)
        materialized = result.credentials

        # Get temp files for cleanup
        temp_files: List[str] = []
        if hasattr(protocol, "get_temp_files"):
            temp_files = protocol.get_temp_files()

        return CredentialHandle(
            slot_name=slot_name,
            values=materialized,
            workflow_id=workflow_id,
            protocol_type=type(protocol).__name__,
            temp_files=temp_files,
        )


class CredentialActivityInboundInterceptor(ActivityInboundInterceptor):
    """Activity interceptor that sets up credential context.

    Sets the credential context before activity execution so activities
    can access ctx.http and ctx.credentials.
    """

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute activity with credential context.

        Sets up the credential context from the workflow's stored credentials.
        """
        from temporalio import activity

        info = activity.info()
        workflow_id = info.workflow_id

        # Set up credential context if credentials are initialized
        store = WorkerCredentialStore.get_instance()
        if store.is_initialized(workflow_id):
            ctx = create_credential_context(workflow_id)
            set_credential_context(ctx)
        else:
            set_credential_context(None)

        try:
            return await super().execute_activity(input)
        finally:
            # Clear context after activity
            set_credential_context(None)


class CredentialInterceptor(Interceptor):
    """Main interceptor for credential injection.

    Provides credential bootstrap at workflow start and context access
    in activities.

    Flow:
    1. JWT is passed via Temporal headers (x-credential-jwt)
    2. Heracles validates JWT and returns slot mappings with field mappings
    3. Secrets are fetched from Dapr secret store (abstracts Vault, AWS SM, etc.)
    4. Field mappings transform secret store fields to app-expected fields
    5. CredentialHandles and HTTP clients are created
    6. Activities access via ctx.http and ctx.credentials

    Usage:
        >>> from application_sdk.interceptors.credential import CredentialInterceptor
        >>> from application_sdk.credentials import Credential, AuthMode
        >>>
        >>> # Define credentials in handler
        >>> credentials = [
        ...     Credential(name="api", auth=AuthMode.API_KEY),
        ...     Credential(name="database", auth=AuthMode.DATABASE),
        ... ]
        >>>
        >>> # Add interceptor to worker
        >>> interceptor = CredentialInterceptor(credentials)
        >>> worker = Worker(..., interceptors=[interceptor])
    """

    def __init__(
        self,
        credential_declarations: Optional[List[Credential]] = None,
    ) -> None:
        """Initialize the credential interceptor.

        Args:
            credential_declarations: List of credential declarations.
                If provided, protocols are resolved from declarations.
                If not provided, defaults are used.
        """
        self._credential_declarations = credential_declarations or []

    def workflow_interceptor_class(
        self,
        input: WorkflowInterceptorClassInput,
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Get the workflow interceptor class.

        Returns a class that captures the credential declarations.
        """
        declarations = self._credential_declarations

        class ConfiguredCredentialWorkflowInboundInterceptor(
            CredentialWorkflowInboundInterceptor
        ):
            def __init__(self, next: WorkflowInboundInterceptor) -> None:
                super().__init__(next, declarations)

        return ConfiguredCredentialWorkflowInboundInterceptor

    def intercept_activity(
        self,
        next: ActivityInboundInterceptor,
    ) -> ActivityInboundInterceptor:
        """Intercept activity executions to provide credential context."""
        return CredentialActivityInboundInterceptor(next)
