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

### 2. Install uv 0.7.3 and Python

uv manages both Python environments and dependencies:

```bash
# Install UV
curl -LsSf https://astral.sh/uv/0.7.3/install.sh | sh

# Install Python 3.11.10
uv venv --python 3.11.10

# activate the venv
source .venv/bin/activate

# Verify installation
python --version # Should show Python 3.11.10
```

### 3. Install Temporal CLI

Temporal is the workflow orchestration platform:

```bash
brew install temporal
```

### 4. Install DAPR CLI

DAPR (Distributed Application Runtime) simplifies microservice development:

```bash
curl -fsSL https://raw.githubusercontent.com/dapr/cli/master/install/install.sh | /bin/bash -s 1.14.1
dapr init --runtime-version 1.13.6 --slim
```

> [!NOTE]
> Your development environment is now ready! Head over to our [Getting Started Guide](../guides/getting-started.md) to learn how to:
> - Install project dependencies
> - Run example applications

For common setup issues, please see our [Troubleshooting Guide](https://github.com/atlanhq/application-sdk/blob/main/docs/docs/setup/troubleshooting.md).