---
title: macOS Setup Guide for Application SDK
description: Step-by-step instructions for setting up the Application SDK on macOS
tags:
  - setup
  - macos
  - installation
---

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

### 2. Install Python 3.11

We'll use pyenv to manage Python versions:

```bash
# Install pyenv
brew install pyenv

# Set up shell environment
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.zshrc
echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.zshrc
echo 'eval "$(pyenv init -)"' >> ~/.zshrc
source ~/.zshrc

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

Temporal is the workflow orchestration platform:

```bash
brew install temporal
```

### 5. Install DAPR CLI

DAPR (Distributed Application Runtime) simplifies microservice development:

```bash
curl -fsSL https://raw.githubusercontent.com/dapr/cli/master/install/install.sh | /bin/bash -s 1.14.1
dapr init --runtime-version 1.13.6 --slim
```

### 6. Install Project Dependencies

Install all required dependencies:

```bash
make install

# Activate the virtual environment if not already activated
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