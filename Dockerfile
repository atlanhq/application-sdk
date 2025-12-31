FROM cgr.dev/atlan.com/app-framework-golden:3.13.11

# Dapr version argument
ARG DAPR_VERSION=1.16.3

# Switch to root for installation
USER root

# Install Dapr CLI (latest version for apps to use)
RUN curl -fsSL https://raw.githubusercontent.com/dapr/cli/master/install/install.sh | DAPR_INSTALL_DIR="/usr/local/bin" /bin/bash -s ${DAPR_VERSION}


# Create appuser (standardized user for all apps)
RUN addgroup -g 1000 appuser && adduser -D -u 1000 -G appuser appuser

# Set up directories for apps
RUN mkdir -p /app /home/appuser/.local/bin /home/appuser/.cache/uv && \
    chown -R appuser:appuser /app /home/appuser

# Remove curl and bash (no longer needed) and clean apk cache
RUN apk del curl bash && rm -rf /var/cache/apk/*

# Switch to appuser before dapr init and venv creation
USER appuser

# Default working directory for applications
WORKDIR /app

# Initialize Dapr (slim mode) for apps
RUN dapr init --slim --runtime-version=${DAPR_VERSION}

# Remove dashboard, placement, and scheduler from Dapr - not needed and have vulnerabilities
RUN rm -f /home/appuser/.dapr/bin/dashboard /home/appuser/.dapr/bin/placement /home/appuser/.dapr/bin/scheduler 2>/dev/null || true

# Common environment variables for all apps
ENV UV_CACHE_DIR=/home/appuser/.cache/uv \
    XDG_CACHE_HOME=/home/appuser/.cache \
    ATLAN_DAPR_HTTP_PORT=3500 \
    ATLAN_DAPR_GRPC_PORT=50001 \
    ATLAN_DAPR_METRICS_PORT=3100

# Default command (can be overridden by extending images)
CMD ["python"]

