FROM ghcr.io/atlanhq/pyatlan-chainguard-base:8.3.0-3.11

# Switch to root for installation
USER root

WORKDIR /tmp/build

# Install Dapr CLI
RUN curl -fsSL https://raw.githubusercontent.com/dapr/cli/master/install/install.sh | DAPR_INSTALL_DIR="/usr/local/bin" /bin/bash -s 1.16.0

# Set UV environment variables
ENV UV_NO_MANAGED_PYTHON=true \
    UV_SYSTEM_PYTHON=true

# Install build dependencies (git is required for cloning)
RUN apk add --no-cache git


# Clone application-sdk source from GitHub
RUN echo "=== Cloning application-sdk from GitHub ===" && \
    git clone https://github.com/atlanhq/application-sdk.git /tmp/build/application-sdk && \
    cd /tmp/build/application-sdk && \
    echo "Repository cloned successfully"


# # Install missing dependencies that aren't available in Chainguard APK registry
# # These core dependencies must be installed via pip/uv
# This section will have all the optional dependencies for the application-sdk
RUN uv pip install --system \
    "opentelemetry-exporter-otlp==1.39.0" \
    "fastapi[standard]>=0.120.2" \
    "loguru>=0.7.3,<0.8.0" \
    "uvloop>=0.21.0,<0.23.0" \
    "python-dotenv>=1.1.0,<1.3.0" \
    "duckdb>=1.1.3,<1.5.0" \
    "duckdb-engine>=0.17.0,<0.18.0" \
    "aiohttp>=3.10.0,<3.14.0" \
    "psutil>=7.0.0,<7.2.0" \
    "pydantic>=2.10.6,<2.13.0"

# Optional dependencies for the application-sdk
RUN uv pip install --system \
    "dapr==1.16.0" \
    "temporalio>=1.7.1,<1.21.0" \
    "orjson>=3.10.18,<3.12.0" \
    "pandas>=2.2.3,<2.4.0" \
    "daft>=0.4.12,<0.7.0" \
    "pyiceberg>=0.8.1,<0.11.0" \
    "sqlalchemy[asyncio]>=2.0.36,<2.1.0" \
    "boto3>=1.38.6,<1.43.0" \
    "pytest-order>=1.3.0,<1.4.0" \
    "pandera[io]>=0.23.1,<0.28.0" \
    "pyyaml>=6.0.2,<6.1.0" \
    "pyarrow>=20.0.0,<23.0.0" \
    "faker>=37.1.0,<38.3.0" \
    "numpy>=1.23.5,<2.4.0" \
    "redis[hiredis]>=5.2.0,<7.2.0" \
    "fastmcp>=2.12.3,<2.14.0"


# Build and install pyatlan from source (NO PyPI!)
RUN cd /tmp/build/application-sdk && \
    echo "Building application-sdk from source..." && \
    uv pip install --system --no-deps .

# Verify installation
RUN python3 -c "\
import application_sdk; \
import sys; \
print('=== Application SDK Installation Verification ==='); \
print(f'Version: {application_sdk.__version__}'); \
print(f'Location: {application_sdk.__file__}'); \
"

# Clean up build artifacts
RUN rm -rf /tmp/build

# Remove curl and bash
RUN apk del curl bash

# Switch back to nonroot user
USER nonroot

# Default working directory for applications
WORKDIR /app

# Default command (can be overridden by extending images)
CMD ["python"]
