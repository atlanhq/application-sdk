# Environment Variables: Removal and Rename Process

When you remove or rename an env var the SDK reads, deployment manifests
out in the wild may still set the old name. Without a signal, the value
silently no-ops — observability "works" but ignores the user's config.

The SDK warns at startup when any removed/renamed env var is still set in
the environment. To make sure your removal shows up in that warning, add
the **old** env var name to the registry.

## Runtime vs test-harness variables

The SDK reads two kinds of environment variable, and the rules on this page apply
to only one of them.

| | Runtime variables | Test-harness variables |
|---|---|---|
| Prefix | `ATLAN_` (ADR-0009). Exempt: `OTEL_`, `DAPR_`, `K8S_` | `E2E_` |
| Read by | App code, on production paths | Only code under `application_sdk/testing/` |
| Set by | Helm charts, Dockerfiles, `ConfigMap`s on deployed apps | The SDK's reusable CI workflows and scripts, or a developer running tests locally |
| Documented in | [`docs/configuration.md`](../configuration.md) | The harness docstrings and [`connector-ci-e2e.md`](connector-ci-e2e.md) |
| Removal / rename | Registry below, with a startup warning | No registry. Nothing deployed sets them, and the startup warning runs in `run_main()`, which the test harness never calls |

A test-harness variable describes a test run: which tenant, which pool, which
worker to wait for, whether a source is available. A deployed app never reads one.
That is why it does **not** take the `ATLAN_` prefix. The prefix marks a variable
an operator can set on a production app, and giving it to a CI knob would advertise
it as one.

Two rules keep the line clear:

- **Never read an `E2E_*` variable outside `application_sdk/testing/`.** If
  production code needs the value, it is a runtime variable: name it `ATLAN_*` and
  add it to `docs/configuration.md`.
- **Never give a test-harness variable the `ATLAN_` prefix.** CI-only config
  belongs in the `E2E_*` namespace beside its siblings (`E2E_SOURCE_AVAILABLE`,
  `E2E_TEMPORAL_ADDRESS`, `E2E_TENANT_DEPLOYMENT_NAME`, `E2E_TENANT_POOL`, …).

To list every test-harness variable in use:
`grep -rhoE '"E2E_[A-Z_]+' application_sdk/testing | sort -u`.

## The registry

`application_sdk/common/env_warnings.py`:

```python
_REMOVED_ENV_VARS: frozenset[str] = frozenset(
    {
        # short comment on why this was removed
        "OLD_ENV_VAR_NAME",
        ...
    }
)
```

The startup warning is emitted by `warn_removed_env_vars()`, called once
from `run_main()` (`application_sdk/main.py`).

## When to add an entry

Add the **old** env var name when:

- **Removing** an env var (the controlled feature is gone).
  - Example: `ATLAN_ENABLE_APP_VITALS` after the App Vitals interceptor was
    folded into `LogInterceptor`.
- **Renaming** an env var **without** a fallback read of the old name.
  - The user sees their old var ignored; the warning tells them.

Do NOT add an entry when:

- The env var is **renamed but still read with fallback** (e.g.,
  `ATLAN_TEMPORAL_HOST` falls back to `ATLAN_WORKFLOW_HOST`). The old name
  is still functional, no warning needed.
- The env var is **moved** (e.g., from `constants.py` into `AppConfig`)
  but still read by the SDK. Verify with grep before adding:
  ```sh
  grep -rE "\"OLD_ENV_VAR_NAME\"" application_sdk/ --include="*.py"
  ```
  If any non-test hit remains, it's still alive — don't add.

## What the user sees

A single warning line at startup listing every detected old var:

```
WARNING The SDK no longer reads these env vars (set in environment, values
ignored): ATLAN_ENABLE_APP_VITALS, OTEL_WORKFLOW_LOGS_ENDPOINT. Consult the
SDK / changelog for current equivalents.
```

We deliberately don't track replacements in the registry — the dev can
read the SDK or changelog to find the new equivalent.

### The warning is awareness, not a defect

Seeing this warning **does not mean the deployment is misconfigured**.
A single Helm chart often spans multiple SDK versions (older deployments
still on a previous SDK release that did read the old var, newer
deployments on the current SDK that doesn't). Keeping the old var set is
the correct behaviour for backwards compatibility — the warning just
makes sure the author *knows* the new SDK isn't reading it, so they can
also add the newer equivalent (when one exists) and let the old var fade
out as older deployments retire.

If the var has no replacement (the controlling feature was removed
entirely), the chart can drop it whenever the last SDK version that read
it is retired.

---

## Kubernetes-injected variables

These env vars are **not** set by the app or the SDK — they are injected by the
Kubernetes Downward API in the pod spec. The SDK reads them but cannot validate
that they were injected; when absent, the dependent feature is silently
disabled.

### `K8S_POD_MEMORY_LIMIT`

Fallback limit for the memory-pressure observability feature (startup RSS
baseline log, heartbeat WARNING at ≥ 80 % of limit). The SDK reads the
container's cgroup limit first; this variable is used only when the cgroup
reports none, so most pods need not set it.

**Inject via:**

```yaml
env:
  - name: K8S_POD_MEMORY_LIMIT
    valueFrom:
      resourceFieldRef:
        resource: limits.memory
        divisor: "1"   # plain bytes string; no suffix
```

**Accepted formats** (the SDK's `parse_pod_memory_limit()` helper accepts all of these):

| Format | Example | Notes |
|--------|---------|-------|
| Raw bytes | `4294967296` | What `resourceFieldRef` with `divisor: "1"` produces |
| Binary SI | `4Gi`, `512Mi`, `1Ti` | Ki / Mi / Gi / Ti / Pi / Ei (powers of 1024) |
| Decimal SI | `4G`, `512M`, `1T` | k / M / G / T / P / E (powers of 1000) |

Any other value (including floats like `1.5Gi`) is treated as 0 (feature disabled).

### `K8S_POD_NAME`

The pod's name as assigned by Kubernetes. Used in diagnostic log messages
(e.g., the entrypoint's exit-137 SIGKILL trailer).

**Inject via:**

```yaml
env:
  - name: K8S_POD_NAME
    valueFrom:
      fieldRef:
        fieldPath: metadata.name
```

### `K8S_POD_NAMESPACE`

The pod's namespace. Used in diagnostic log messages alongside `K8S_POD_NAME`.

**Inject via:**

```yaml
env:
  - name: K8S_POD_NAMESPACE
    valueFrom:
      fieldRef:
        fieldPath: metadata.namespace
```

---

## Checklist when removing/renaming an env var

1. Remove the read site (`os.getenv(...)` / `os.environ.get(...)`).
2. Confirm no other read site exists:
   ```sh
   grep -rE "\"OLD_ENV_VAR_NAME\"" application_sdk/ --include="*.py"
   ```
3. Add the old name to `_REMOVED_ENV_VARS` in
   `application_sdk/common/env_warnings.py` with a one-line comment on why.
4. Update any docs that reference the old name
   (`docs/configuration.md`, `docs/guides/deployment.md`, etc.).
5. Mention the change in the PR description so the changelog flags it.
