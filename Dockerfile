FROM cgr.dev/atlan.com/app-framework-golden:3.13

# Dapr runtime package from Chainguard APK
ARG DAPR_RUNTIME_PACKAGE=dapr-daprd-1.17

# Switch to root for installation
USER root

# Install Dapr runtime from Chainguard APK and remove attack-surface tools
RUN apk add --no-cache ${DAPR_RUNTIME_PACKAGE} && \
    apk del curl bash && \
    rm -rf /var/cache/apk/*

# Create appuser (standardized user for all apps)
RUN addgroup -g 1000 appuser && adduser -D -u 1000 -G appuser appuser

# Set up directories for apps
RUN mkdir -p /app /home/appuser/.local/bin && \
    chown -R appuser:appuser /app /home/appuser

# Switch to appuser before venv creation
USER appuser

# Default working directory for applications
WORKDIR /app

# Common environment variables for all apps
ENV UV_NO_CACHE=1 \
    ATLAN_DAPR_HTTP_PORT=3500 \
    ATLAN_DAPR_GRPC_PORT=50001 \
    ATLAN_DAPR_METRICS_PORT=3100 \
    DAPR_LOG_LEVEL=info \
    DAPR_APP_ID=app \
    DAPR_MAX_BODY_SIZE="1024Mi" \
    DAPR_MAX_BODY_SIZE_MB=1024 \
    DO_NOT_TRACK=true \
    SCARF_NO_ANALYTICS=true \
    DAFT_ANALYTICS_ENABLED=0

# Copy entrypoint script for graceful shutdown handling
COPY --chown=appuser:appuser entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["sh", "/usr/local/bin/entrypoint.sh"]
