"""The preflight scenario matrix F016 requires every app to define.

Plain data with no imports, so the static suite can read it without pytest
installed; ``conformance.preflight_testing`` re-exports it as ``SCENARIOS``.
"""

from __future__ import annotations

F016_SCENARIOS: tuple[str, ...] = (
    "healthy",
    "mandatory_failure",
    "advisory_failure",
    "recoverable_transient",
    "persistent_failure",
    "mixed_resources",
    "extraction_fallback",
    "credential_entrypoint_shapes",
    "no_probe",
    "hung_probe",
    "cancellation_cleanup",
    "budget_retry",
    "typed_safe_output",
)

SCENARIOS: dict[str, tuple[str, ...]] = {"F016": F016_SCENARIOS}
