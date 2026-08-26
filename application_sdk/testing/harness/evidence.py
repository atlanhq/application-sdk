"""Collecting what a failed run left behind, and redacting it before it ships.

A red CI leg is only useful if the evidence for it survives the pod that
produced it. This module collects that evidence into one bundle — pod logs,
the DAG node table, the counts that were read, the findings — and redacts it on
the way out.

Redaction is not optional and not a caller's responsibility. The harness handles
credential bodies (a connector's source credentials, an Atlan API key, a
tenant hostname) and evidence is the one thing here that is *designed* to leave
the process. Redaction therefore happens at this boundary, not by withholding
fields from the types upstream: withholding makes the domain objects harder to
debug locally while still leaking anything a future field forgets to withhold.

Implementation, and wiring it into the connector failure path, is child G on
FND-224.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from application_sdk.testing.harness._errors import HarnessNotBuiltError
from application_sdk.testing.harness.expectations import Finding

__all__ = ["EvidenceBundle", "redact"]


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceBundle:
    """Everything worth keeping about one harness run.

    Attributes:
        label: What the run was — the suite and entrypoint, for the report title.
        findings: Every unmet expectation, accumulated rather than truncated at
            the first.
        logs: Source name -> captured lines. Source is a pod name, a container,
            or a synthetic name for a non-pod source.
        readings: Named observations the run made — asset counts, node states,
            poller identities. Kept as a mapping rather than typed per kind so a
            new observation does not need a new field here.
        artifacts: Relative path -> file contents to write alongside the report.
    """

    label: str
    findings: Sequence[Finding] = field(default_factory=tuple)
    logs: Mapping[str, Sequence[str]] = field(default_factory=dict)
    readings: Mapping[str, object] = field(default_factory=dict)
    artifacts: Mapping[str, str] = field(default_factory=dict)


def redact(bundle: EvidenceBundle) -> EvidenceBundle:
    """Return *bundle* with credential-shaped values replaced by placeholders.

    Args:
        bundle: The bundle to sanitise.

    Returns:
        A new bundle. Never mutates the input: a caller that logs locally and
        uploads remotely must be able to hold both, and an in-place scrub makes
        the local copy useless.

    Raises:
        HarnessNotBuiltError: Always — implementation is child G on FND-224.
    """
    raise HarnessNotBuiltError(
        message="redact is not implemented yet",
        operation="redact",
        reason="child G on FND-224",
        issue="FND-224",
        component="harness_evidence",
    )
