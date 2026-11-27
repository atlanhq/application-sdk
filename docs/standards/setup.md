# Setup Overview

This page is the one list of what the Atlan Application SDK needs on your machine. The OS guides below say how to install it; other pages link here instead of repeating it.

## Required

| Tool | Version | Why |
|---|---|---|
| Python | 3.11, 3.12, 3.13 or 3.14 | `requires-python = ">=3.11,<3.15"`; CI runs the unit tests on all four. 3.15 is not supported yet: `rocksdict` has no 3.15 wheel. See [Supported Python window](ci.md#the-supported-python-window-is-capped-fnd-5476) |
| uv | latest | Installs Python and the dependencies, and runs every command (`uv run ...`) |

That is all you need to build an app, run it locally with `run_dev_combined()`, and run the SDK's unit and integration tests. The SDK downloads the Dapr runtime (`daprd`, at the version it pins) and an in-process Temporal server the first time they are needed, into `~/.cache/atlan-sdk/`.

## Optional: external Temporal and Dapr

To run an app against a standalone Temporal server and a Dapr sidecar, as production does, you also need:

| Tool | Version | Used by |
|---|---|---|
| [Temporal CLI](https://docs.temporal.io/cli#install) | latest | `uv run poe start-deps` / `poe start-temporal`, and the external-infrastructure section of [Getting Started](../guides/getting-started.md#optional-run-against-external-dapr-temporal) |

You do not need the Dapr CLI or `dapr init`. `poe start-dapr` runs the SDK-pinned `daprd` binary directly, the same way production does.

## OS-Specific Setup Guides

Follow the guide for your operating system:

- [Mac](../setup/MAC.md)
- [Linux](../setup/LINUX.md)
- [Windows](../setup/WINDOWS.md)

Once your environment is ready, follow [Getting Started](../guides/getting-started.md) to build and run your first app. The commands for working on the SDK itself (tests, pre-commit, docs) are in [Development Commands](../agents/dev-commands.md).

## Need Help?

If you encounter issues during setup, see [Troubleshooting](../setup/troubleshooting.md) or reach out at connect@atlan.com.
