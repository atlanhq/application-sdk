---
title: Linux Setup Guide for Application SDK
description: Step-by-step instructions for setting up the Application SDK on Linux
tags:
  - setup
  - linux
  - installation
---

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

### 2. Install Python 3.11 with pyenv

We'll use pyenv to manage Python versions:

```bash
# Install pyenv
curl https://pyenv.run | bash

# Add pyenv to your path
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
echo 'eval "$(pyenv init -)"' >> ~/.bashrc
source ~/.bashrc

# Install and set Python 3.11.10
pyenv install 3.11.10
pyenv global 3.11.10

# Verify installation
python --version  # Should show Python 3.11.10
```

### 3. Install uv 0.7.3

uv manages Python dependencies and project environments:

```bash
curl -LsSf https://astral.sh/uv/0.7.3/install.sh | sh
```

### 4. Install Temporal CLI

Temporal is used for workflow orchestration:

```bash
curl -sSf https://temporal.download/cli.sh | sh
export PATH="$HOME/.temporalio/bin:$PATH"
echo 'export PATH="$HOME/.temporalio/bin:$PATH"' >> ~/.bashrc
```

### 5. Install DAPR CLI

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
> Now you have your environment ready. You can now start setting up project dependencies.
> The following steps will guide you through running the examples.


### 6. Install Project Dependencies

- Install all required dependencies:

```bash
uv sync --all-extras --all-groups
```

- Setup pre-commit hooks

```bash
uv run pre-commit install
```

### 7. Start the dependencies in a separate terminal:

- Download the components

```bash
uv run poe download-components
```

- Start the dependencies

```bash
uv run poe start-deps
```

### 8. Run the example application in the main terminal:

```bash
uv run examples/application_hello_world.py
```

