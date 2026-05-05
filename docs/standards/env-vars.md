# Environment Variables: Removal and Rename Process

When you remove or rename an env var the SDK reads, deployment manifests
out in the wild may still set the old name. Without a signal, the value
silently no-ops — observability "works" but ignores the user's config.

The SDK warns at startup when any removed/renamed env var is still set in
the environment. To make sure your removal shows up in that warning, add
the **old** env var name to the registry.

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
