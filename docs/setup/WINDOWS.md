# Windows Setup Guide

This guide will help you set up the Application SDK on Windows.

## Prerequisites

Before starting, ensure you have:
  - Windows 10 or higher
  - PowerShell access (run as Administrator)
  - Internet connection

## Setup Steps

### 1. Install uv and Python

Install uv using PowerShell:

```powershell
# Install uv
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Create a virtualenv on a supported Python (3.11 to 3.14; see the Setup Overview)
uv venv --python 3.12

# Verify installation
uv run python --version # Should show Python 3.12.x
```

### 2. That's it: Dapr and Temporal are auto-managed

You do **not** need the Dapr or Temporal CLIs to build, run or test an app; the SDK
downloads what it needs. What is required, and the optional extras for running
against external infrastructure, are listed once in the
[Setup Overview](../standards/setup.md).

> [!NOTE]
> Your development environment is now ready! Head over to our [Getting Started Guide](../guides/getting-started.md) to learn how to:
> - Install project dependencies
> - Run example applications

For common setup issues, please see our [Troubleshooting Guide](https://github.com/atlanhq/application-sdk/blob/main/docs/setup/troubleshooting.md).