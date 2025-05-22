# Getting Started with Application SDK

This guide will help you get started with Application SDK development. Follow these steps sequentially, skipping any steps you've already completed.

## Core Dependencies

The Application SDK requires the following core dependencies:

- [**Python 3.11**](https://www.python.org/downloads/release/python-31110/): The runtime environment for the SDK
- [**UV**](https://docs.astral.sh/uv/): Modern Python package manager and build system for fast, reproducible installations
- [**Dapr**](https://docs.dapr.io/): Distributed Application Runtime that simplifies microservice development with built-in state management, pub/sub, and more
- [**Temporal**](https://docs.temporal.io/): Workflow engine that handles complex, long-running processes with built-in reliability

> [!NOTE]
> Dapr and Temporal work together to provide a complete platform - Dapr handles microservice communication and state management, while Temporal orchestrates complex workflows and ensures their reliability.

## Step 1: Set Up Development Environment

> [!TIP]
> If you already have Python 3.11, UV, Dapr, and Temporal installed, you can skip to Step 2.

Choose your platform-specific setup guide to install all required dependencies:

- [macOS Setup Guide](../setup/MAC.md)
- [Linux Setup Guide](../setup/LINUX.md)
- [Windows Setup Guide](../setup/WINDOWS.md)

## Step 2: Run Your First Application

### Setting Up Project Dependencies

1. Install project dependencies:
   ```bash
   uv sync --all-extras --all-groups
   ```

2. Set up pre-commit hooks:
   ```bash
   uv run pre-commit install
   ```

3. Download required components:
   ```bash
   uv run poe download-components
   ```

### Running the Example

1. Start the dependencies (in a separate terminal):
   ```bash
   uv run poe start-deps
   ```

2. Run an example application:
   ```bash
   uv run python examples/application_hello_world.py
   ```

3. Navigate to [localhost:8233](http://localhost:8233) to see a completed workflow :sparkles:


4. (optional) once you're done, stop the dependencies if you don't need them anymore:
   ```bash
   uv run poe stop-deps
   ```

## Step 3: Advanced Configuration (Optional)

> [!NOTE]
> This step is only needed if you're:
> - Connecting to remote Temporal or Dapr services
> - Need custom configuration for your development environment
> - Setting up production environments

If you need to customize your environment:

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

2. Configure your environment variables in `.env`:
   - Set Temporal connection details (for remote Temporal service)
   - Configure Dapr endpoints (for custom Dapr setup)
   - Adjust any other required settings

For detailed configuration options, refer to our [Configuration Guide](../configuration.md).

## Next Steps

After successfully running your first application, explore these resources to learn more:

- Explore our [SQL Application Guide](./sql-application-guide.md) for building data applications
- Learn about our [Architecture](./architecture.md)
- Review our [Best Practices](./best-practices.md)
- Check out our [Test Framework](./test-framework.md) for testing your applications