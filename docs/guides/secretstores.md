# Secret Stores and Credentials

This guide covers the full credential lifecycle in the Application SDK: how credentials flow from Dapr secret stores into typed Python objects that your app code uses safely.

For the concise API reference, see [Credentials](../concepts/credentials.md).

---

## How Credentials Flow

```
External store           Dapr sidecar         SDK resolver          App code
(Vault, K8s, AWS SM) ──► DaprSecretStore ──► CredentialResolver ──► BasicCredential
                                                                      ApiKeyCredential
                                                                      ...
```

1. Helm / Kubernetes provisions a Dapr secret-store component pointing at the external store.
2. The SDK's `DaprSecretStore` calls the Dapr sidecar API to read the secret JSON.
3. `CredentialResolver` deserialises the JSON into a typed `Credential` model.
4. App code receives a strongly-typed object — no `dict["password"]` access patterns.

---

## Typed Credentials

All credential types are frozen Pydantic models available from `application_sdk.credentials`:

| Type | Fields | Factory ref |
|------|--------|-------------|
| `BasicCredential` | `username`, `password` | `basic_ref(name)` |
| `ApiKeyCredential` | `api_key`, `header_name`, `prefix` | `api_key_ref(name)` |
| `BearerTokenCredential` | `token`, `expires_at` | `bearer_token_ref(name)` |
| `OAuthClientCredential` | `client_id`, `client_secret`, `token_url`, `scopes`, `access_token` | `oauth_client_ref(name)` |
| `CertificateCredential` | `cert_data`, `key_data`, `ca_data`, `passphrase` | `certificate_ref(name)` |
| `AtlanApiToken` | `token`, `base_url` | `atlan_api_token_ref(name)` |
| `AtlanOAuthClient` | `client_id`, `client_secret`, `base_url` | `atlan_oauth_client_ref(name)` |
| `GitSshCredential` | `key_data`, `passphrase` | `git_ssh_ref(name)` |
| `GitTokenCredential` | `token` | `git_token_ref(name)` |
| `RawCredential` | `data` (raw dict) | `legacy_credential_ref(guid)` — migration only |

### Using credentials in a task

```python
from application_sdk.credentials import basic_ref, BasicCredential
from application_sdk.app import App, task

class MyConnector(App):
    @task
    async def fetch_data(self, input: FetchInput) -> FetchOutput:
        ref = basic_ref("my-db-creds")
        cred: BasicCredential = await self.context.resolve_credential(ref)
        conn = await connect(host=input.host, user=cred.username, password=cred.password)
        ...
```

`self.context.resolve_credential(ref)` reads from the injected `SecretStore` (Dapr in production, `MockSecretStore` in tests) and returns the typed object.

---

## Dapr Component Configuration

In production, Dapr components map component names to external secret backends. The SDK reads from a component named `secretstore` by default (`SECRET_STORE_NAME` env var).

### Kubernetes Secrets (default for local / CI)

```yaml
# components/secretstore.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: secretstore
spec:
  type: secretstores.kubernetes
  version: v1
```

### AWS Secrets Manager

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: secretstore
spec:
  type: secretstores.aws.secretsmanager
  version: v1
  metadata:
    - name: region
      value: "us-east-1"
    - name: accessKey
      secretKeyRef:
        name: aws-creds
        key: access_key_id
    - name: secretKey
      secretKeyRef:
        name: aws-creds
        key: secret_access_key
```

### HashiCorp Vault

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: secretstore
spec:
  type: secretstores.hashicorp.vault
  version: v1
  metadata:
    - name: vaultAddr
      value: "https://vault.internal:8200"
    - name: vaultToken
      secretKeyRef:
        name: vault-token
        key: token
```

See the [Dapr secret store documentation](https://docs.dapr.io/reference/components-reference/supported-secret-stores/) for the full list of supported backends.

---

## Deployment Secrets

A second secret store, `deployment-secret-store` (`DEPLOYMENT_SECRET_STORE_NAME`), holds environment-specific secrets such as Temporal auth credentials. The SDK reads from it automatically when `ATLAN_AUTH_ENABLED=true`.

The key names within the deployment secret are configurable:

```bash
ATLAN_AUTH_CLIENT_ID_KEY=ATLAN_AUTH_CLIENT_ID      # default
ATLAN_AUTH_CLIENT_SECRET_KEY=ATLAN_AUTH_CLIENT_SECRET
```

---

## Testing Without Dapr

Inject a `MockSecretStore` in tests to avoid needing a Dapr sidecar:

```python
import pytest
from application_sdk.testing import MockSecretStore
from application_sdk.infrastructure import InfrastructureContext, set_infrastructure, clear_infrastructure

@pytest.fixture
def infra():
    ctx = InfrastructureContext(
        secret_store=MockSecretStore({
            "my-db-creds": '{"type":"basic","username":"admin","password":"secret"}',
        }),
    )
    set_infrastructure(ctx)
    yield ctx
    clear_infrastructure()

async def test_connect(infra):
    connector = MyConnector()
    output = await connector.fetch_data(FetchInput(host="localhost"))
    assert output.rows > 0
```

For richer credential test data, use `MockCredentialStore`:

```python
from application_sdk.testing import MockCredentialStore
from application_sdk.infrastructure import InfrastructureContext, set_infrastructure

store = MockCredentialStore()
ref = store.add_basic("my-db-creds", username="admin", password="secret")
ctx = InfrastructureContext(secret_store=store.secret_store)
set_infrastructure(ctx)
# Always pair set_infrastructure with clear_infrastructure() in teardown,
# or use a yield fixture to avoid test pollution:
#   @pytest.fixture(autouse=True)
#   def infra():
#       set_infrastructure(ctx)
#       yield
#       clear_infrastructure()
```

---

## End-User Guide: AWS Secrets Manager

If your app supports AWS Secrets Manager as an external credential source through the Atlan UI, users can set it up as follows.

### Creating a secret

1. Open AWS Secrets Manager in the AWS Console.
2. Click **Store a new secret** → **Other type of secret**.
3. Add key-value pairs matching the credential fields your connector expects:
   - Basic auth: `username`, `password`
   - IAM User: `username`, `access-key-id`, `secret-access-key`
   - IAM Role: `username`, `role-arn`, `external-id`
4. Give the secret a descriptive name and save.
5. Copy the Secret ARN (format: `arn:aws:secretsmanager:region:account-id:secret:name`).

### Configuring in the Atlan UI

In your connector's configuration form:
1. Select **AWS Secrets Manager** as the credential source.
2. Enter the Secret ARN and AWS Region.
3. Map form fields to key names within the secret (e.g. `password` → `db_password` if your secret uses that key).

The platform will retrieve and inject the credential values at workflow start time.

### Troubleshooting AWS credential failures

- **Access denied**: The IAM role used by the Dapr sidecar must have `secretsmanager:GetSecretValue` permission for the secret ARN.
- **Wrong region**: The region in the Dapr component config must match the secret's region.
- **Key name mismatch**: Form field key names must exactly match the keys inside the AWS secret.
- **Stale cache**: Dapr caches secrets for a configurable TTL. Rotate secrets and wait for the cache to expire, or restart the Dapr sidecar.

Check AWS CloudTrail for access-denied audit events.
