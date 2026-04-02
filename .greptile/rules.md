## Cross-Repository Impact

This SDK is consumed by downstream applications including `atlan-redshift-app`.
When reviewing changes, always consider:

- **Breaking changes**: Modifications to public classes, methods, or their signatures
  must be backward-compatible or go through a deprecation cycle.
- **Behavioral changes**: Even non-breaking signature changes can alter runtime behavior.
  Flag any semantic changes to ObjectStore, StateStore, SecretStore, EventStore,
  Reader/Writer, or workflow orchestration logic.
- **Configuration changes**: Changes to default values, environment variable names,
  or Dapr component schemas affect all downstream apps.

## SDK Public API Surface

The following modules form the public API consumed by downstream apps:

- `application_sdk.application` — BaseApplication, application lifecycle
- `application_sdk.clients` — ClientInterface and implementations
- `application_sdk.handlers` — HandlerInterface and implementations
- `application_sdk.activities` — ActivitiesInterface and implementations
- `application_sdk.workflows` — WorkflowInterface and implementations
- `application_sdk.services` — ObjectStore, StateStore, SecretStore, EventStore
- `application_sdk.io` — Reader, Writer abstractions

Any change to these modules should be reviewed with downstream impact in mind.
