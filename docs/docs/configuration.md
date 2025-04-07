# Configuration

The application uses the following environment variables for configuration:


## Temporal Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `ATLAN_TEMPORAL_HOST` | Specifies the host of the Temporal server | `localhost` |
| `ATLAN_TEMPORAL_PORT` | Specifies the port on which the Temporal server is running | `7233` |
| `ATLAN_TEMPORAL_NAMESPACE` | Specifies the Temporal namespace for the application | `default` |
| `ATLAN_APPLICATION_NAME` | Specifies the application name for identifying the Temporal client | `default` |

## Tenant Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `ATLAN_TENANT_ID` | Specifies the tenant identifier for the application | `development` |
