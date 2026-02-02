FROM cgr.dev/atlan.com/app-framework-golden:3.13.11

# Dapr version arguments
ARG DAPR_CLI_VERSION=1.16.5
ARG DAPR_RUNTIME_PACKAGE=dapr-daprd-1.16

# Switch to root for installation
USER root

# Install Dapr CLI (latest version for apps to use)
RUN curl -fsSL https://raw.githubusercontent.com/dapr/cli/master/install/install.sh | DAPR_INSTALL_DIR="/usr/local/bin" /bin/bash -s ${DAPR_CLI_VERSION}


# Install Dapr runtime from Chainguard APK (0 CVEs vs upstream Dapr releases)
RUN apk add --no-cache ${DAPR_RUNTIME_PACKAGE}

# Create appuser (standardized user for all apps)
RUN addgroup -g 1000 appuser && adduser -D -u 1000 -G appuser appuser

# Set up directories for apps
RUN mkdir -p /app /home/appuser/.local/bin /home/appuser/.cache/uv && \
    chown -R appuser:appuser /app /home/appuser

# Remove curl and bash (no longer needed) and clean apk cache
RUN apk del curl bash && rm -rf /var/cache/apk/*

# Switch to appuser before venv creation
USER appuser

# Default working directory for applications
WORKDIR /app

# Ensure Dapr directories exist for components/runtime
RUN mkdir -p /home/appuser/.dapr/components /home/appuser/.dapr/bin && \
    ln -s /usr/bin/daprd /home/appuser/.dapr/bin/daprd && \
    cat <<'EOF' > /home/appuser/.dapr/config.yaml
apiVersion: dapr.io/v1alpha1
kind: Configuration
metadata:
  name: daprConfig
spec: {}
EOF

# Common environment variables for all apps
ENV UV_CACHE_DIR=/home/appuser/.cache/uv \
    XDG_CACHE_HOME=/home/appuser/.cache \
    ATLAN_DAPR_HTTP_PORT=3500 \
    ATLAN_DAPR_GRPC_PORT=50001 \
    ATLAN_DAPR_METRICS_PORT=3100 \
    DAPR_LOG_LEVEL=info \
    DAPR_APP_ID=app \
    DAPR_MAX_BODY_SIZE="1024Mi"

# Copy entrypoint script for graceful shutdown handling
COPY --chown=appuser:appuser entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["sh", "/usr/local/bin/entrypoint.sh"]
