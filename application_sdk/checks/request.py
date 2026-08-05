"""What a check run needs, independent of how the request arrived.

Four callers ask the same question through four very different envelopes: an HTTP
body shaped by two generations of the Heracles wire format, an SDR Temporal
workflow argument, a secret-free routing envelope built inside a deterministic
workflow, and (on the scheduled path) whatever the Automation Engine sends. They
used to each assemble the handler's ``PreflightInput`` themselves, and they did it
differently — which is why the same connection could produce different checks
depending on which button the customer pressed.

:class:`CheckRequest` is the one shape they all normalize into. Wire compatibility
stays at the edges where it belongs: the HTTP boundary keeps its v2-format
normalizers, the gate keeps its snapshot envelope, and both then build a
``CheckRequest``. Nothing downstream of here can tell which path it came from —
except through :attr:`CheckRequest.trigger`, which is deliberately explicit so the
outcome row can say what prompted the run.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from application_sdk.checks.credentials import CredentialSource
from application_sdk.checks.depth import CheckDepth
from application_sdk.contracts.base import SerializableEnum
from application_sdk.handler.contracts import (
    BaseConnectionConfig,
    BaseMetadataConfig,
    HandlerCredential,
    PreflightInput,
)


class CheckTrigger(SerializableEnum):
    """What prompted a check run.

    Stamped on the outcome row so the same verdict stream can answer questions
    that were previously unanswerable: is this app's config UI green while its
    runs are red? Did drift detection see this coming before the run failed? Only
    the pre-run trigger is subject to gate enforcement — the rest report.
    """

    UI_AUTH = "ui_auth"
    """"Test authentication" in the config UI — a run capped at ``AUTH`` depth."""

    UI_PREFLIGHT = "ui_preflight"
    """The config UI's full preflight, run while a human waits on the result."""

    SDR = "sdr"
    """A connectivity test against an app running on customer infrastructure."""

    PRE_RUN = "pre_run"
    """The mandatory gate before extraction — the only enforcing caller."""

    SCHEDULED = "scheduled"
    """Proactive drift detection, with nobody waiting on the answer."""


class CheckRequest(BaseModel):
    """One check run's inputs, normalized across every ingress."""

    app_name: str = ""
    """The app being checked — attributed on the outcome row and on failures."""

    entrypoint: str = ""
    """Bare entry-point name, for apps whose checks differ per entrypoint."""

    trigger: CheckTrigger = CheckTrigger.UI_PREFLIGHT
    """What prompted this run. Reported, never used to decide the verdict."""

    depth: CheckDepth = CheckDepth.FULL
    """How deep to go — see :class:`~application_sdk.checks.depth.CheckDepth`."""

    credential_source: CredentialSource = Field(default_factory=CredentialSource)
    """How to obtain the credential: inline values, or a reference to dereference."""

    metadata_config: dict[str, Any] = Field(default_factory=dict)
    """Form-level metadata, as a raw mapping — becomes ``PreflightInput.metadata``."""

    connection_config: dict[str, Any] = Field(default_factory=dict)
    """Connection configuration — becomes ``PreflightInput.connection_config``.

    Kept **separate** from :attr:`metadata_config` rather than merged into one
    mapping. The HTTP boundary mirrors one onto the other when only one is sent, but
    a caller that sends both with different contents means both: merging them would
    silently hand every handler the union, which is not what either field said.
    Callers with a single view of the form (the pre-run gate, working from an
    extraction snapshot) pass the same mapping for both.
    """

    checks_to_run: list[str] = Field(default_factory=list)
    """Connector-specific check names (empty = all). Prefer :attr:`depth`."""

    budget_seconds: float = 60.0
    """Wall-clock seconds the handler gets, before credential resolution is
    deducted. The runner enforces it and reports the *remaining* figure to the
    handler, so what we enforce and what we tell the handler are the same number.
    """

    def to_preflight_input(
        self,
        credentials: list[HandlerCredential],
        credentials_by_name: dict[str, list[HandlerCredential]],
        handler_budget_seconds: int,
    ) -> PreflightInput:
        """Build the handler-facing input, once credentials are resolved.

        ``handler_budget_seconds`` is what is *left* of the budget, not the
        nominal figure — a handler sizing its probes to this value is sizing to
        the deadline actually enforced.
        """
        return PreflightInput(
            credentials=credentials,
            credentials_by_name=credentials_by_name,
            entrypoint=self.entrypoint,
            metadata=BaseMetadataConfig(**self.metadata_config),
            connection_config=BaseConnectionConfig(**self.connection_config),
            checks_to_run=list(self.checks_to_run),
            depth=self.depth,
            timeout_seconds=handler_budget_seconds,
        )

    @classmethod
    def from_preflight_input(
        cls,
        input: PreflightInput,
        *,
        app_name: str = "",
        trigger: CheckTrigger = CheckTrigger.UI_PREFLIGHT,
        budget_seconds: float | None = None,
        depth: CheckDepth | None = None,
    ) -> CheckRequest:
        """Build a request from an already-validated ``PreflightInput``.

        The HTTP and SDR ingresses take this route: their wire formats are
        validated into a ``PreflightInput`` at the boundary (keeping the v2-compat
        normalizers where they belong), and the credential fields it carries —
        inline ``credentials`` plus an optional ``agent_json`` reference — map
        straight onto a :class:`CredentialSource`.

        ``depth`` overrides what the input declares, for a caller that knows
        better than its own wire format: ``test_auth`` arrives as an ``AuthInput``
        with no depth field and must cap at ``AUTH``.
        """
        return cls(
            app_name=app_name,
            entrypoint=input.entrypoint,
            trigger=trigger,
            depth=depth if depth is not None else input.depth,
            credential_source=CredentialSource(
                inline=list(input.credentials),
                agent_json=input.agent_json,
                # An agent spec is a *reference*, so mark the routing mode that
                # makes CredentialRef.resolve pick the agent path when inline
                # credentials are absent.
                extraction_method="agent" if input.agent_json is not None else "",
            ),
            # Forwarded as sent. The HTTP boundary has already mirrored one onto the
            # other when only one arrived; when both arrived, both are meant.
            metadata_config=input.metadata.model_dump(),
            connection_config=input.connection_config.model_dump(),
            checks_to_run=list(input.checks_to_run),
            budget_seconds=(
                budget_seconds
                if budget_seconds is not None
                else float(input.timeout_seconds)
            ),
        )


_ROUTING_KEYS: frozenset[str] = frozenset(
    {"extraction_method", "credential_guid", "agent_json", "credential_ref"}
)


_EMPTY_CONFIG_VALUES: tuple[Any, ...] = (None, "", (), [], {})


def config_from_snapshot(
    snapshot: dict[str, Any], drop_keys: Iterable[str] = ()
) -> dict[str, Any]:
    """Extract check form config from a raw extraction-input snapshot.

    The gate path's input assembly. Called inside the activity frame (never in the
    deterministic workflow) so that app-authored field reads cannot run in a
    non-deterministic context on replay. Produces both the original field name and
    its hyphenated equivalent, because handlers in the fleet use either convention
    and the gate must work for both.

    Drops credential-routing fields, any ``drop_keys`` (named-credential guid
    fields, which are references rather than form config), and *genuinely* empty
    values — but preserves ``False`` and ``0``, so a handler reading a bool/int
    config field sees the real value instead of silently falling back to a default.
    """
    dropped = _ROUTING_KEYS | set(drop_keys)
    config: dict[str, Any] = {}
    for k, v in snapshot.items():
        if k in dropped or v in _EMPTY_CONFIG_VALUES:
            continue
        config[k] = v
        if "_" in k:
            config[k.replace("_", "-")] = v
    return config
