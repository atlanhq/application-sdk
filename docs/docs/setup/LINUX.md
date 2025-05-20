# Linux Setup Guide

This guide will help you set up the Application SDK on Linux (Ubuntu/Debian based systems).

## Prerequisites

Before starting, ensure you have:
  - Terminal access
  - Sudo privileges (for installing software)
  - Internet connection

## Setup Steps

### 1. Install System Dependencies

First, install essential build dependencies:

```bash
sudo apt-get update
sudo apt-get install -y make build-essential libssl-dev zlib1g-dev \
libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm \
libncurses5-dev libncursesw5-dev xz-utils tk-dev libffi-dev \
liblzma-dev
```

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

Temporal is used for workflow orchestration:

```bash
curl -sSf https://temporal.download/cli.sh | sh
export PATH="$HOME/.temporalio/bin:$PATH"
echo 'export PATH="$HOME/.temporalio/bin:$PATH"' >> ~/.bashrc
```

### 4. Install DAPR CLI

Install DAPR using the following commands:

```bash
# Install DAPR CLI
wget -q https://raw.githubusercontent.com/dapr/cli/master/install/install.sh -O - | /bin/bash -s 1.14.1

# Initialize DAPR (slim mode)
dapr init --runtime-version 1.13.6 --slim

# Verify installation
dapr --version
```

> [!NOTE]
> Your development environment is now ready! Head over to our [Getting Started Guide](../guides/getting-started.md) to learn how to:
> - Install project dependencies
> - Run example applications

For common setup issues, please see our [Troubleshooting Guide](https://github.com/atlanhq/application-sdk/blob/main/docs/docs/setup/troubleshooting.md).