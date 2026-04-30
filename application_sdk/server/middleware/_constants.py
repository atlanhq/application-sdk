"""Shared constants for middleware modules."""

# Paths excluded from logging and metrics — covers both v2 (/server/health)
# and v3 (/health) health probe paths.
EXCLUDED_LOG_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/server/health",
        "/ready",
        "/server/ready",
        "/api/eventingress/",
        "/api/eventingress",
        "/metrics",
    }
)
