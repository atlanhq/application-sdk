---
title: Windows Setup Guide for Application SDK
description: Step-by-step instructions for setting up the Application SDK on Windows
tags:
  - setup
  - windows
  - installation
---

# Windows Setup Guide

This guide will help you set up the Application SDK on Windows.

## Prerequisites

Before starting, ensure you have:
      - PowerShell access (run as Administrator)
      - Internet connection
      - Windows 10 or higher

## Setup Steps

### 1. Install Python 3.11.10

Download and install Python from the official website:

1. Go to [Python Downloads](https://www.python.org/downloads/release/python-31110/)
2. Download the Windows installer (64-bit)
3. Run the installer with these options:
      - Add Python to PATH
      - Install for all users
      - Customize installation
      - All optional features
      - Install to a directory without spaces (e.g., `C:\Python311`)
4. Verify installation by opening PowerShell and running:
   ```powershell
   python --version  # Should show Python 3.11.10
   ```

### 2. Install uv 0.7.3

Install uv using PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.7.3/install.ps1 | iex"
```

### 3. Install Temporal CLI

Download and install Temporal:

```powershell
# Create a directory for Temporal CLI
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.temporalio\bin"

# Download Temporal CLI
Invoke-WebRequest -Uri https://temporal.download/cli/archive/latest?platform=windows&arch=amd64 -OutFile "$env:USERPROFILE\.temporalio\temporal.zip"

# Extract and install
Expand-Archive -Path "$env:USERPROFILE\.temporalio\temporal.zip" -DestinationPath "$env:USERPROFILE\.temporalio\bin" -Force

# Add to PATH
$env:Path += ";$env:USERPROFILE\.temporalio\bin"
[Environment]::SetEnvironmentVariable("Path", $env:Path, [System.EnvironmentVariableTarget]::User)

# Verify installation
temporal --version
```

### 4. Install DAPR CLI

Install DAPR using PowerShell:

```powershell
# Install DAPR CLI
powershell -Command "$script=iwr -useb https://raw.githubusercontent.com/dapr/cli/master/install/install.ps1; $block=[ScriptBlock]::Create($script); invoke-command -ScriptBlock $block -ArgumentList 1.14.1"

# Initialize DAPR (slim mode)
dapr init --runtime-version 1.13.6 --slim

# Verify installation
dapr --version
```

### 5. Install Project Dependencies

- Install all required dependencies:

```powershell
uv sync --all-groups
```

- Setup pre-commit hooks

```powershell
uv run pre-commit install
```

### 6. Start the dependencies in a separate terminal:

```powershell
# Download the components
uv run poe download-components

# Start all services in detached mode
uv run poe start-deps
```

### 7. Run the example application

```powershell
# Run the example application in the main terminal:
uv run python examples/application_hello_world.py
```
