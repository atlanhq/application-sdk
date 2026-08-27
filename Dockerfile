FROM cgr.dev/atlan.com/app-framework-golden:3.13

# Switch to root for setup
USER root

# Create appuser (standardized user for all apps)
RUN addgroup -g 1000 appuser && adduser -D -u 1000 -G appuser appuser

# Set up directories for apps
RUN mkdir -p /app /home/appuser/.local/bin && \
    chown -R appuser:appuser /app /home/appuser

# Remove curl and bash (not needed at runtime) and clean apk cache
RUN apk del curl bash && rm -rf /var/cache/apk/*

# Drop the pip/setuptools bootstrap wheels.
#
# `py3-pip-wheel` and `py3-setuptools-wheel` are hard dependencies of the
# python-3.x-base packages, so `apk del` refuses them (verified: it removes 0
# packages and leaves Python intact). The wheel FILES, however, are only read by
# `ensurepip` when bootstrapping a virtual environment -- and nothing here does
# that. Dependencies are installed by `uv`, which is self-contained and never
# touches these files.
#
# Removing them clears five findings that customer scanners report against this
# base, all of which resolve to the wheel file or to a manifest inside it rather
# than to anything installed:
#   CVE-2018-20225   pip         -> the wheel file itself
#   CVE-2026-57585   msgpack     -> pip/_vendor/bom.cdx.json inside the wheel
#   CVE-2026-23949   setuptools  -> same manifest
#   CVE-2025-47273   setuptools  -> same manifest
#   CVE-2026-59890   setuptools  -> same manifest
# The manifest lists pip's vendored dependency versions (setuptools 70.3.0,
# msgpack 1.1.2); neither is installed anywhere in the image.
#
# Caveat: this removes the files, not the apk package records. A scanner that
# reads the apk database rather than file contents will still report
# py3-pip-wheel. That is a known limit of this change, not an oversight -- the
# records cannot be removed without taking Python with them.
#
# Trade-off: `python -m venv` and `python -m ensurepip` no longer work inside the
# image. Nothing in the SDK or any connector app uses either (the one `-m venv`
# in this repo runs on a GitHub runner, not in the container).
RUN rm -f /usr/share/python-wheels/pip-*.whl \
          /usr/share/python-wheels/setuptools-*.whl

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
    DAFT_ANALYTICS_ENABLED=0 \
    ATLAN_CONTRACT_GENERATED_DIR=/app/app/generated

# Copy entrypoint script for graceful shutdown handling
COPY --chown=appuser:appuser entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

COPY --chown=appuser:appuser CHANGELOG.md /opt/atlan/application-sdk/CHANGELOG.md

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
