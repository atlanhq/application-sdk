FROM cgr.dev/atlan.com/app-framework-golden:3.13

# Dapr version arguments
ARG DAPR_CLI_VERSION=1.17.0
ARG DAPR_RUNTIME_PACKAGE=dapr-daprd-1.17.2

# Switch to root for installation
USER root

# Install Dapr CLI (latest version for apps to use)
RUN curl -fsSL https://raw.githubusercontent.com/dapr/cli/master/install/install.sh | DAPR_INSTALL_DIR="/usr/local/bin" /bin/bash -s ${DAPR_CLI_VERSION}


# Install Dapr runtime from Chainguard APK
RUN apk add --no-cache ${DAPR_RUNTIME_PACKAGE}

# Create appuser (standardized user for all apps)
RUN addgroup -g 1000 appuser && adduser -D -u 1000 -G appuser appuser

# Set up directories for apps
RUN mkdir -p /app /home/appuser/.local/bin && \
    chown -R appuser:appuser /app /home/appuser

# Remove curl and bash (no longer needed at runtime) and clean apk cache.
# Note: bash is still available during the build steps above (Dapr CLI install).
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
ENV UV_NO_CACHE=1 \
    DAPR_HTTP_PORT=3500 \
    DAPR_GRPC_PORT=50001 \
    DAPR_LOG_LEVEL=info \
    DAPR_APP_ID=app \
    DAPR_METRICS_PORT=3100 \
    DAPR_MAX_BODY_SIZE=1024Mi \
    DAPR_GRACEFUL_SHUTDOWN_SECONDS=3600 \
    DO_NOT_TRACK=true \
    SCARF_NO_ANALYTICS=true \
    DAFT_ANALYTICS_ENABLED=0

# Copy entrypoint script for graceful shutdown handling
COPY --chown=appuser:appuser entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
