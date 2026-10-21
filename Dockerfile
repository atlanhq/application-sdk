FROM cgr.dev/atlan.com/app-framework-golden:3.13

# Switch to root for setup
USER root

# The golden image now ships the Dapr runtime (dapr-daprd-1.18), which
# conflicts with installing dapr-daprd-1.17 on top. Use the golden image's
# daprd, as main does, and remove attack-surface tools. ca-certificates-bundle
# is raised in place because the golden image pins 20260909-r2 in
# /etc/apk/world (so a plain `apk upgrade` is a no-op) and lags the fix for
# CVE-2026-111943 (20260909-r3); the build gate blocks on HIGH.
RUN apk add --no-cache "ca-certificates-bundle>=20260909-r3" && \
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
    DAPR_MAX_BODY_SIZE_MB="1024Mi" \
    DO_NOT_TRACK=true \
    SCARF_NO_ANALYTICS=true \
    DAFT_ANALYTICS_ENABLED=0

# Point grpcio at the image's system CA bundle.
#
# The PyPI `grpcio` wheel ships its own `roots.pem` and uses it instead of the
# system trust store, so a fix to `ca-certificates-bundle` in the base image
# never reaches it. That left gRPC-over-TLS clients (e.g. Google Cloud SDKs)
# trusting the compromised emSign ECC Root CA - G3 after the base was rebuilt
# on 20260909-r3 (GHSA-6q48-6c7w-9gw2). With this set, grpcio follows the same
# bundle as OpenSSL (SSL_CERT_FILE), so trust-store fixes land in one place.
#
# Only the default roots change: a client that passes its own
# `root_certificates` (e.g. a customer-supplied CA) is unaffected.
ENV GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/ca-certificates.crt

# Copy entrypoint script for graceful shutdown handling
COPY --chown=appuser:appuser entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["sh", "/usr/local/bin/entrypoint.sh"]
