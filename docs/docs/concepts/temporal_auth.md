# Temporal Worker Authentication

This document describes the authentication system for Temporal workers in the Application SDK.

## Overview

The Application SDK provides a robust OAuth2-based authentication system for Temporal workers using the client credentials flow. This system enables secure communication between your application and the Temporal server with automatic credential discovery and token management.

## Authentication Components

### AtlanAuthClient

The `AtlanAuthClient` class is the core component that handles all authentication operations:

- **Token Management**: Acquisition, refresh, and caching of OAuth2 access tokens
- **Credential Discovery**: Automatic discovery from secret stores with environment variable fallback
- **Security**: Implements best practices for credential rotation and secure token handling

### SecretStoreInput

Provides automatic Dapr component discovery and secret retrieval:

- **Component Discovery**: Automatically finds available secret store components
- **Secret Retrieval**: Fetches credentials from discovered secret stores
- **Caching**: Caches component discovery results to minimize API calls

### Key Features

1. **Dynamic Token Management**
   - Intelligent token refresh with dynamic interval calculation based on token expiry
   - Automatic token refresh at 80% of token lifetime (minimum 5 minutes, maximum 30 minutes)
   - Intelligent token caching to reduce auth server load
   - Graceful handling of token refresh failures

2. **Smart Credential Discovery**
   - **Primary**: Dapr secret store component (configurable backend)
   - **Flexible Backend**: Support for environment variables, AWS Secrets Manager, Azure Key Vault, etc.
   - **Application-specific**: Uses application name for credential key generation

3. **Production-Ready Security**
   - No hardcoded credentials in application code
   - Secure credential storage through Dapr secret stores
   - Support for credential rotation without application restart
   - Comprehensive error handling and logging

## Configuration

### Dapr Secret Store Component Configuration

The authentication system uses a Dapr secret store component that can be configured to use various backends. The component name is `deployment-secret-store` and can be configured to use:

- **Environment Variables**: `secretstores.local.env`
- **AWS Secrets Manager**: `secretstores.aws.secretmanager`
- **Azure Key Vault**: `secretstores.azure.keyvault`
- **HashiCorp Vault**: `secretstores.hashicorp.vault`
- **Local File**: `secretstores.local.file`

**Environment Variables:**
```bash
# Authentication settings
ATLAN_WORKFLOW_AUTH_ENABLED=true
ATLAN_WORKFLOW_AUTH_URL=https://your-oauth-provider.com/oauth/token

# Secret store component configuration
ATLAN_DEPLOYMENT_SECRET_COMPONENT=deployment-secret-store
ATLAN_DEPLOYMENT_SECRET_NAME=atlan-deployment-secrets

# Temporal connection settings
ATLAN_WORKFLOW_HOST=temporal.your-domain.com
ATLAN_WORKFLOW_PORT=7233
ATLAN_WORKFLOW_NAMESPACE=default
```

### Secret Store Configuration

**Component Setup:**
- **Component Name**: `deployment-secret-store` (configurable via `ATLAN_DEPLOYMENT_SECRET_COMPONENT`)
- **Secret Key**: `atlan-deployment-secrets` (configurable via `ATLAN_DEPLOYMENT_SECRET_NAME`)
- **Credential Format**: Application-specific keys within the secret

**Example Secret Structure:**
```json
{
  "postgres_extraction_client_id": "your_client_id_here",
  "postgres_extraction_client_secret": "your_client_secret_here",
  "query_intelligence_client_id": "query_intel_client_id",
  "query_intelligence_client_secret": "query_intel_client_secret"
}
```

**Key Naming Convention:**
- Format: `<app_name>_client_id` and `<app_name>_client_secret`
- App name transformation: lowercase with hyphens converted to underscores
- Example: "postgres-extraction" → "postgres_extraction_client_id"

### Component Configuration Examples

**Environment Variables Backend:**
```yaml
# components/deployment-secret-store.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: deployment-secret-store
spec:
  type: secretstores.local.env
  version: v1
  metadata:
  - name: prefix
    value: "ATLAN_"
```

**AWS Secrets Manager Backend:**
```yaml
# components/deployment-secret-store.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: deployment-secret-store
spec:
  type: secretstores.aws.secretmanager
  version: v1
  metadata:
  - name: region
    value: "us-east-1"
  - name: accessKey
    value: ""
  - name: secretKey
    value: ""
```

**Azure Key Vault Backend:**
```yaml
# components/deployment-secret-store.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: deployment-secret-store
spec:
  type: secretstores.azure.keyvault
  version: v1
  metadata:
  - name: vaultName
    value: "my-key-vault"
```

## Usage Examples

### Basic Authentication Setup

```python
from application_sdk.clients.temporal import TemporalWorkflowClient

# Initialize client with authentication
client = TemporalWorkflowClient(
    application_name="postgres-extraction",
    auth_enabled=True,
    auth_url="https://auth.company.com/oauth/token"
)

# Establish authenticated connection
await client.load()
```

### Development Setup (Environment Variables Backend)

```python
# For development/testing with environment variables backend
# Configure the deployment-secret-store component to use secretstores.local.env
# and set environment variables with ATLAN_ prefix

client = TemporalWorkflowClient(
    application_name="query-intelligence",
    auth_enabled=True,
    auth_url="https://auth.company.com/oauth/token"
)

await client.load()
```

### Worker Creation

```python
# Create authenticated worker
worker = client.create_worker(
    activities=[my_activity_function],
    workflow_classes=[MyWorkflowClass],
    passthrough_modules=["my_custom_module"]
)

# Run the worker
await worker.run()
```

### Manual Token Management

```python
from application_sdk.clients.atlan_auth import AtlanAuthClient

# Create auth client directly
# Credentials are automatically fetched from the configured secret store component
auth_client = AtlanAuthClient()
```

# Get token for external API calls
token = await auth_manager.get_access_token()
headers = await auth_manager.get_authenticated_headers()

# Use with HTTP requests
async with aiohttp.ClientSession() as session:
    await session.get("https://api.company.com/data", headers=headers)
```

## Authentication Flow

### 1. Client Initialization
- `TemporalWorkflowClient` creates an `AtlanAuthClient` instance
- Configuration loaded from constructor parameters and environment variables

### 2. Credential Discovery (Automatic)
- **Environment Variables**: First checks for credentials in environment variables
- **Secret Store Fallback**: Falls back to Dapr secret stores if environment variables not available
- **Secret Store Discovery**: Uses Dapr metadata API to find available secret stores
- **Credential Retrieval**: Attempts to fetch app-specific credentials from secret store
- **Credential Caching**: Caches discovered credentials for subsequent use

### 3. Token Acquisition
- Uses OAuth2 client credentials flow to obtain access token
- Token includes all scopes configured for the application in the OAuth provider
- Implements intelligent caching with expiry tracking

### 4. Temporal Connection
- Includes Bearer token in gRPC metadata for Temporal server authentication
- Automatic token refresh on subsequent operations

### 5. Dynamic Token Refresh (Automatic)
- Calculates optimal refresh interval based on token expiry time
- Refreshes at 80% of token lifetime (minimum 5 minutes, maximum 30 minutes)
- Recalculates interval on each refresh to adapt to changing token lifetimes
- Handles refresh failures by clearing credential cache and retrying
- Supports credential rotation without application restart

## Error Handling

The authentication system provides comprehensive error handling:

### Common Error Scenarios

```python
try:
    await client.load()
except ValueError as e:
    # Credential configuration issues
    if "OAuth2 credentials not found" in str(e):
        logger.error("Check environment variables first, then secret store")
    else:
        logger.error(f"Configuration error: {e}")

except ConnectionError as e:
    # Network/connectivity issues
    logger.error(f"Cannot connect to auth server: {e}")

except Exception as e:
    # Token refresh or other auth failures
    logger.error(f"Authentication failed: {e}")
    # Potentially retry or use fallback mechanism
```

### Error Recovery

```python
# Example: Retry with credential cache clear
async def connect_with_retry(client, max_retries=3):
    for attempt in range(max_retries):
        try:
            await client.load()
            return
        except Exception as e:
            if attempt < max_retries - 1:
                # Clear caches and retry
                client.auth_manager.clear_cache()
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
            else:
                raise
```

## Best Practices

### 1. Credential Management
- **Production**: Use environment variables for primary credentials, Dapr secret stores as fallback
- **Development**: Environment variables for testing and development
- **Security**: Never commit credentials to version control
- **Rotation**: Implement regular credential rotation

### 2. Error Handling
- Implement retry logic with exponential backoff
- Log authentication failures for monitoring
- Have fallback mechanisms for critical operations
- Monitor authentication metrics

### 3. Performance
- Cache tokens appropriately (done automatically)
- Minimize unnecessary auth server calls
- Use connection pooling for HTTP clients

### 4. Monitoring
- Track token refresh frequency
- Monitor authentication failure rates
- Set up alerts for credential expiry
- Log secret store discovery issues

## Troubleshooting

### Authentication Failures

**Symptom**: `ValueError: OAuth2 credentials not found`
```bash
# Check secret store
kubectl get components  # Verify Dapr secret store component
kubectl logs <your-pod> | grep "secret store"

# Check secret store component configuration
kubectl get components deployment-secret-store -o yaml

# Check if credentials are accessible via Dapr
dapr invoke --app-id your-app --method get-secret --data '{"key": "atlan-deployment-secrets"}'
```

**Symptom**: Token refresh failures
```bash
# Verify auth URL accessibility
curl -X POST $ATLAN_WORKFLOW_AUTH_URL \
  -d "grant_type=client_credentials&client_id=...&client_secret=..."

# Check credential validity in secret store
```

### Secret Store Issues

**Symptom**: `No secret store components found`
```bash
# Check Dapr component configuration
kubectl get components -o yaml

# Verify component has type starting with 'secretstores.'
# Example: secretstores.kubernetes, secretstores.azure.keyvault
```

**Symptom**: `Failed to fetch secret using component`
```bash
# Test Dapr secret access directly
dapr invoke --app-id your-app --method health
kubectl logs dapr-sidecar-container
```

### Connection Issues

**Symptom**: gRPC connection failures
```bash
# Verify Temporal server accessibility
telnet $ATLAN_WORKFLOW_HOST $ATLAN_WORKFLOW_PORT

# Check if token is being included in requests
# Enable debug logging to see gRPC metadata
```

## API Reference

### AtlanAuthClient

```python
class AtlanAuthClient:
    """OAuth2 token manager for cloud service authentication."""

    def __init__(
        self,
        application_name: str,
        auth_enabled: bool | None = None,
        auth_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        """Initialize OAuth2 token manager."""

    async def get_access_token(self, force_refresh: bool = False) -> str:
        """Get valid access token, refreshing if necessary."""

    async def get_authenticated_headers(self) -> Dict[str, str]:
        """Get authentication headers for HTTP requests."""

    async def is_token_valid(self) -> bool:
        """Check if current token is valid (not expired)."""

    def get_token_expiry_time(self) -> Optional[float]:
        """Get the expiry time of the current token."""

    def get_time_until_expiry(self) -> Optional[float]:
        """Get the time remaining until token expires."""

    async def refresh_token(self) -> str:
        """Force refresh the access token."""

    def clear_cache(self) -> None:
        """Clear cached credentials and token."""

    def get_application_name(self) -> str:
        """Get the application name."""

    def is_auth_enabled(self) -> bool:
        """Check if authentication is enabled."""
```

### TemporalWorkflowClient

```python
class TemporalWorkflowClient(WorkflowClient):
    """Temporal-specific implementation of WorkflowClient."""

    def __init__(
        self,
        host: str | None = None,
        port: str | None = None,
        application_name: str | None = None,
        namespace: str | None = "default",
        auth_enabled: bool | None = None,
        auth_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        """Initialize Temporal workflow client."""

    async def load(self) -> None:
        """Connect to Temporal server with authentication."""

    async def close(self) -> None:
        """Close client connection and clear auth cache."""

    async def start_workflow(
        self,
        workflow_args: Dict[str, Any],
        workflow_class: Type[WorkflowInterface]
    ) -> Dict[str, Any]:
        """Start a workflow execution."""

    def create_worker(
        self,
        activities: Sequence[CallableType],
        workflow_classes: Sequence[ClassType],
        passthrough_modules: Sequence[str],
        max_concurrent_activities: Optional[int] = None,
        activity_executor: Optional[ThreadPoolExecutor] = None,
        auto_start_token_refresh: bool = True,
    ) -> Worker:
        """Create Temporal worker with authenticated client."""
```

### SecretStoreInput

```python
class SecretStoreInput:
    """Secret store integration with automatic component discovery."""

    @classmethod
    async def get_secret(
        cls,
        component_name: str,
        secret_key: str
    ) -> Dict[str, Any]:
        """Fetch secret using Dapr component."""

    @classmethod
    def discover_secret_component(
        cls,
        use_cache: bool = True
    ) -> Optional[str]:
        """Discover available secret store component."""

    @classmethod
    def clear_discovery_cache(cls) -> None:
        """Clear component discovery cache."""
```

## Migration Guide

### From Manual Credential Management

**Before:**
```python
client = TemporalWorkflowClient()
# Manual token management required
```

**After:**
```python
client = TemporalWorkflowClient(
    application_name="query-intelligence",
    auth_enabled=True
)
await client.load()  # Authentication handled automatically
```

### Secret Store Component Configuration

Configure your Dapr secret store component:

**For Environment Variables Backend:**
```yaml
# components/deployment-secret-store.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: deployment-secret-store
spec:
  type: secretstores.local.env
  version: v1
  metadata:
  - name: prefix
    value: "ATLAN_"
```

**Environment Variables:**
```bash
# Authentication settings
ATLAN_WORKFLOW_AUTH_ENABLED=true
ATLAN_WORKFLOW_AUTH_URL=https://your-oauth-server.com/oauth/token

# Secret store configuration
ATLAN_DEPLOYMENT_SECRET_COMPONENT=deployment-secret-store
ATLAN_DEPLOYMENT_SECRET_NAME=atlan-deployment-secrets

# Credentials (if using environment variables backend)
ATLAN_<app_name>_client_id=your_client_id
ATLAN_<app_name>_client_secret=your_client_secret
```