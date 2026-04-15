"""Test fixture for sdk-review — intentionally contains DESIGN_CHANGE
smells that should trigger NEEDS_HUMAN_REVIEW rather than a simple
PATCH or MIGRATE fix. NEVER merge."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def do_everything(
    user_data: dict[str, Any],
    db_conn: Any,
    cache: Any,
    config: dict[str, Any],
    cloud_provider: Any,
    notifier: Any,
    storage: Any,
    metrics: Any,
    lineage: Any,
    auth: Any,
    audit_logger: Any,
    feature_flags: dict[str, bool],
) -> dict[str, Any]:
    """God-function doing everything in one place — 12 collaborators,
    handles user creation, authentication, DB persistence, cache
    invalidation, cloud resource provisioning, notification, storage
    setup, metrics, lineage tracking, authorization, audit, and
    feature flags all inline. This is a textbook DESIGN_CHANGE smell:
    a real review should reject this shape and ask for decomposition,
    not suggest a PATCH fix.
    """
    # Step 1: auth
    if not auth.verify(user_data.get("token")):
        raise PermissionError("invalid token")

    # Step 2: feature flags
    if not feature_flags.get("enable_new_user_flow", False):
        raise RuntimeError("feature disabled")

    # Step 3: db persistence (inline SQL, tight coupling)
    db_conn.execute(
        "INSERT INTO users (name, email) VALUES (%s, %s)",
        (user_data["name"], user_data["email"]),
    )
    user_id = db_conn.lastrowid

    # Step 4: cache invalidation
    cache.delete_pattern(f"user:{user_data['email']}:*")
    cache.set(f"user:id:{user_id}", user_data, ttl=3600)

    # Step 5: cloud provisioning
    bucket = cloud_provider.create_bucket(f"user-{user_id}")
    cloud_provider.set_lifecycle(bucket, days=30)

    # Step 6: storage setup
    storage.mkdir(f"/users/{user_id}")
    storage.write(f"/users/{user_id}/profile.json", user_data)

    # Step 7: metrics
    metrics.increment("users.created")
    metrics.gauge("users.total", db_conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    # Step 8: lineage
    lineage.record_event(
        "user.created",
        user_id=user_id,
        source="do_everything",
        downstream=["cache", "cloud", "storage"],
    )

    # Step 9: notification
    notifier.send_welcome_email(user_data["email"])
    notifier.publish_event("user.created", {"user_id": user_id})

    # Step 10: audit
    audit_logger.log(
        action="user.create",
        actor=user_data.get("actor", "system"),
        target=user_id,
        metadata={"bucket": bucket, "cache_keys": [f"user:id:{user_id}"]},
    )

    # Step 11: response
    return {
        "user_id": user_id,
        "email": user_data["email"],
        "bucket": bucket,
        "storage_path": f"/users/{user_id}",
    }


# Dependency-direction violation: a low-level utility importing from
# a higher-level orchestration layer (should be the other way around).
# Review should flag this as [STRUCT] — dependency direction.
try:
    from application_sdk.workflows.orchestrator import Orchestrator  # noqa — intentional anti-pattern for test
except ImportError:
    Orchestrator = None


def helper_that_imports_orchestrator() -> Any:
    """Low-level helper reaching up into orchestration — wrong direction."""
    if Orchestrator is None:
        return None
    return Orchestrator().status()
