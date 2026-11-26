# macOS Setup Guide

This guide will help you set up the Application SDK on macOS.

## Prerequisites

Before starting, ensure you have:
  - Terminal access
  - Admin privileges (for installing software)
  - Internet connection

## Setup Steps

### 1. Install Homebrew

Homebrew is a package manager for macOS that simplifies software installation:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow any post-installation instructions shown in the terminal.

### 2. Install uv and Python

uv manages both Python and the project's dependencies:

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create a virtualenv on a supported Python (3.11 to 3.14; see the Setup Overview)
uv venv --python 3.12

# activate the venv
source .venv/bin/activate

# Verify installation
python --version # Should show Python 3.12.x
```

### 3. That's it: Dapr and Temporal are auto-managed

You do **not** need the Dapr or Temporal CLIs to build, run or test an app; the SDK
downloads what it needs. What is required, and the optional extras for running
against external infrastructure, are listed once in the
[Setup Overview](../standards/setup.md).

> [!NOTE]
> Your development environment is now ready! Head over to our [Getting Started Guide](../guides/getting-started.md) to learn how to:
> - Install project dependencies
> - Run example applications

For common setup issues, please see our [Troubleshooting Guide](https://github.com/atlanhq/application-sdk/blob/main/docs/setup/troubleshooting.md).