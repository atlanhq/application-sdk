# 🔐 AWS and Azure Authentication Support

## 📝 Summary
Add native AWS and Azure authentication support to the Application SDK, with extensibility for future GCP integration.

## 💡 Basic Example
```python
from application_sdk.auth import AWSAuth, AzureAuth

# AWS Authentication
aws_auth = AWSAuth(
    region="us-east-1",
    role_arn="arn:aws:iam::123456789012:role/AppRole"
)

# Azure Authentication  
azure_auth = AzureAuth(
    tenant_id="tenant-id",
    client_id="client-id",
    subscription_id="subscription-id"
)

# Use in SQL client
sql_client = BaseSQLClient(
    connection_string="...",
    auth_provider=aws_auth
)
```

## 🎯 Motivation
- **Cloud Integration**: Enable seamless integration with major cloud providers
- **Security**: Use cloud-native authentication mechanisms (IAM roles, managed identities)
- **Flexibility**: Support multiple authentication methods for different deployment scenarios
- **Enterprise Ready**: Meet enterprise requirements for cloud authentication

## 💼 Acceptance Criteria
- [ ] Design authentication provider interface
- [ ] Implement AWS authentication (IAM roles, access keys, STS)
- [ ] Implement Azure authentication (managed identities, service principals)
- [ ] Add authentication to SQL clients and other SDK components
- [ ] Create authentication configuration examples
- [ ] Add comprehensive tests for authentication flows
- [ ] Document authentication setup and best practices
- [ ] Design extensible interface for future GCP support

## 🔧 Technical Requirements
- Support for AWS IAM roles and temporary credentials
- Support for Azure managed identities and service principals
- Integration with existing SDK clients (SQL, object store, etc.)
- Proper error handling and credential refresh
- Configuration through environment variables and config files

## 🏷️ Labels
- `enhancement`
- `authentication`
- `aws`
- `azure`
- `v0.2.0`

## 🔗 Reference
Related to Application SDK v0.2.0 release planning discussion