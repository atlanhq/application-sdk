"""Evidence-key masking for the HTTP wire.

String and value redaction (``redact_secrets``, ``redact_wire_value``,
``sanitize_cause_repr``) is the SDK's own, in :mod:`application_sdk_api.errors.base`,
and is re-exported here so existing callers keep working.

What this module adds is masking *by key name*: secret-named evidence keys are
replaced with ``***`` rather than rejected. ``FailureDetails`` rejects them in its
validator; the serving boundary builds failure details inside a "report
NOT_READY, never 500" path, where a raising validator would turn a redaction
problem into the 500 that boundary exists to prevent.
"""

from __future__ import annotations

# The string/value redactors are the SDK's own; this module adds only the
# evidence-key masking the HTTP surface applies on top.
from application_sdk_api.errors.base import (  # noqa: F401
    redact_secrets,
    redact_wire_value,
    sanitize_cause_repr,
)
from application_sdk_api.errors.wire import (  # noqa: E402,F401
    mask_secret_named_keys,
    secret_named_evidence_keys,
)
