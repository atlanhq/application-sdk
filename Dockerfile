FROM ghcr.io/atlanhq/pyatlan-chainguard-base:8.3.0-3.11

# Switch to root for installation
USER root

# Install Dapr CLI (latest version for apps to use)
RUN curl -fsSL https://raw.githubusercontent.com/dapr/cli/master/install/install.sh | DAPR_INSTALL_DIR="/usr/local/bin" /bin/bash -s 1.16.3

# Set UV environment variables
ENV UV_NO_MANAGED_PYTHON=true \
    UV_SYSTEM_PYTHON=true

# Copy uv binary for apps to use
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Create appuser (standardized user for all apps)
RUN addgroup -g 1000 appuser && adduser -D -u 1000 -G appuser appuser

# Set up directories for apps
RUN mkdir -p /app /home/appuser/.local/bin /home/appuser/.cache/uv && \
    chown -R appuser:appuser /app /home/appuser

# Remove curl and bash (no longer needed)
RUN apk del curl bash

# Switch to appuser before dapr init and venv creation
USER appuser

# Default working directory for applications
WORKDIR /app

# Pre-populate venv with application-sdk and all required dependencies
# Apps will sync into this same venv, reusing packages when versions match
COPY --chown=appuser:appuser pyproject.toml uv.lock README.md ./
COPY --chown=appuser:appuser application_sdk/ ./application_sdk/
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000,gid=1000 \
    uv venv .venv && \
    uv sync --locked --all-extras --all-groups

# Initialize Dapr (slim mode) for apps
RUN dapr init --slim --runtime-version=1.16.2

# Remove dashboard, placement, and scheduler from Dapr - not needed and have vulnerabilities
RUN rm -f /home/appuser/.dapr/bin/dashboard /home/appuser/.dapr/bin/placement /home/appuser/.dapr/bin/scheduler 2>/dev/null || true

# Clean up build files (apps will copy their own)
# Keep .venv as apps will sync into it
RUN rm -rf pyproject.toml uv.lock README.md application_sdk/

# Common environment variables for all apps
ENV UV_CACHE_DIR=/home/appuser/.cache/uv \
    XDG_CACHE_HOME=/home/appuser/.cache \
    ATLAN_DAPR_HTTP_PORT=3500 \
    ATLAN_DAPR_GRPC_PORT=50001 \
    ATLAN_DAPR_METRICS_PORT=3100

# Default command (can be overridden by extending images)
CMD ["python"]
