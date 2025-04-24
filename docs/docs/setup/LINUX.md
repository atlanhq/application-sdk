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

### 3. Install Poetry 1.8.5

Poetry manages Python dependencies and project environments:

```bash
pip install poetry==1.8.5
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

### 6. Install Application SDK

```bash
poetry install --all-extras

# activate the environment
source .venv/bin/activate
```

### 7. Start Services in detached mode

```bash
make start-all
```

### 8. Run the example application

```bash
poetry run python examples/application_hello_world.py
```

