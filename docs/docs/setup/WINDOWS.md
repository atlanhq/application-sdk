# Windows Setup Guide

This guide will help you set up the Application SDK on Windows.

## Prerequisites

Before starting, ensure you have:
  - Windows 10 or higher
  - PowerShell access (run as Administrator)
  - Internet connection

## Setup Steps

### 1. Install uv 0.7.3 and Python

Install uv using PowerShell:

```powershell
# Install UV
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.7.3/install.ps1 | iex"

# Install Python 3.11.10
uv venv --python 3.11.10

# Verify installation
uv run python --version # Should show Python 3.11.10
```

### 2. Install Temporal CLI

Download and install Temporal:

```powershell
# Create a directory for Temporal CLI
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.temporalio\bin"

# Download Temporal CLI
Invoke-WebRequest -Uri "https://temporal.download/cli/archive/latest?platform=windows&arch=amd64" -OutFile "$env:USERPROFILE\.temporalio\temporal.zip"

# if you face issues with architecture, check: https://temporal.io/setup/install-temporal-cli

# Extract and install
Expand-Archive -Path "$env:USERPROFILE\.temporalio\temporal.zip" -DestinationPath "$env:USERPROFILE\.temporalio\bin" -Force

# Add to PATH
$env:Path += ";$env:USERPROFILE\.temporalio\bin"
[Environment]::SetEnvironmentVariable("Path", $env:Path, [System.EnvironmentVariableTarget]::User)

# Verify installation
temporal --version
```

### 3. Install DAPR CLI

Install DAPR using PowerShell:

```powershell
# Set required execution policy
Set-ExecutionPolicy RemoteSigned -scope CurrentUser

# Install DAPR CLI
$script=iwr -useb https://raw.githubusercontent.com/dapr/cli/master/install/install.ps1; $block=[ScriptBlock]::Create($script); invoke-command -ScriptBlock $block -ArgumentList 1.16.0, "$env:USERPROFILE\.dapr\bin\"

# Add to PATH
$env:Path += ";$env:USERPROFILE\.dapr\bin\"
[Environment]::SetEnvironmentVariable("Path", $env:Path, [System.EnvironmentVariableTarget]::User)

# Initialize DAPR (slim mode)
dapr init --runtime-version 1.16.0 --slim

# Verify installation
dapr --version
```

### 4. Setup Frontend (Optional)

> [!NOTE]
> This step is only required if you plan to write and implement config maps for workflow setup forms.

Install the frontend playground for your application:

```powershell
# Create the frontend static directory
New-Item -ItemType Directory -Force -Path "frontend\static"

# Install the app playground
npx @atlanhq/app-playground install-to frontend/static
```

> [!NOTE]
> `frontend/static` is the default directory. You can change this to a different location, but you'll need to update the path in your workflow definition accordingly.

> [!NOTE]
> Your development environment is now ready! Head over to our [Getting Started Guide](../guides/getting-started.md) to learn how to:
> - Install project dependencies
> - Run example applications

For common setup issues, please see our [Troubleshooting Guide](https://github.com/atlanhq/application-sdk/blob/main/docs/docs/setup/troubleshooting.md).