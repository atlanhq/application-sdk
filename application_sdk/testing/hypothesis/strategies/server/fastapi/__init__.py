"""Hypothesis strategies for the v2 FastAPI server's request payloads.

.. deprecated:: 3.56.0
    Every strategy here is deprecated and removed in v4.0 with the rest of
    ``application_sdk.server.fastapi``. They generate v2 request shapes
    (``credentials`` + ``metadata`` dicts, a Dapr event wrapping the v2
    ``Event``) that no v3 route accepts; nothing in the SDK imports them.
"""

import json
import warnings

from hypothesis import strategies as st

from application_sdk.contracts.events import Event

# Strategy for generating auth credentials
_auth_credentials_strategy = st.fixed_dictionaries(
    {
        "authType": st.just("basic"),
        "account_id": st.text(min_size=5),
        "port": st.integers(min_value=1, max_value=65535),
        "role": st.sampled_from(["ACCOUNTADMIN", "SYSADMIN", "USERADMIN"]),
        "warehouse": st.text(
            alphabet=st.characters(),
        ).map(lambda x: x + "_WH"),
    }
)

# Strategy for generating metadata
_metadata_strategy = st.fixed_dictionaries(
    {
        "include-filter": st.one_of(
            st.just("{}"),
            st.dictionaries(
                keys=st.text(min_size=1, alphabet=st.characters()).map(
                    lambda x: f"^{x.strip()}$"
                ),  # Allow leading/trailing space cases
                values=st.lists(
                    st.text(min_size=0, alphabet=st.characters()).map(
                        lambda x: f"^{x.strip()}$"
                    )
                ),
                min_size=1,
            ).map(json.dumps),
        ),
        "exclude-filter": st.one_of(
            st.just("{}"),
            st.dictionaries(
                keys=st.text(min_size=1),
                values=st.lists(st.text(min_size=0)),
                min_size=1,
            ).map(json.dumps),
        ),
        "temp-table-regex": st.one_of(
            st.just(""),
            st.text(min_size=1, alphabet=st.characters()).map(
                lambda x: f"^{x.strip()}_TEMP$"
            ),
        ),
    }
)

# Strategy for generating complete payload
_payload_strategy = st.fixed_dictionaries(
    {
        "credentials": _auth_credentials_strategy,
        "metadata": _metadata_strategy,
    }
)

# Strategy for generating workflow events
_workflow_event_strategy = st.builds(
    Event,
    event_type=st.just("application_event"),
    event_name=st.just("workflow_start"),
    data={},
)

# Strategy for generating complete event data
_event_data_strategy = st.fixed_dictionaries(
    {
        "data": _workflow_event_strategy,
        "datacontenttype": st.just("application/json"),
        "id": st.uuids().map(str),
        "pubsubname": st.just("pubsub"),
        "source": st.just("workflow-engine"),
        "specversion": st.just("1.0"),
        "time": st.datetimes().map(lambda dt: dt.isoformat() + "Z"),
        "topic": st.just("workflow-events"),
        "traceid": st.text(min_size=32),
        "traceparent": st.text(min_size=32),
        "tracestate": st.just(""),
        "type": st.just("com.dapr.event.sent"),
    }
)


#: name -> (replacement, why). Served by __getattr__ below (PEP 562).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "auth_credentials_strategy": (
        "a strategy built from your app's own credential model",
        "it generates the v2 server's credential dict",
    ),
    "metadata_strategy": (
        "a strategy built from your app's own metadata model",
        "it generates the v2 server's include/exclude-filter dict",
    ),
    "payload_strategy": (
        "a strategy built from your app's typed Input contract",
        "it generates the v2 server's credentials+metadata payload",
    ),
    "workflow_event_strategy": (
        "a strategy built from your app's own event model",
        "it generates the v2 server's workflow-start Event",
    ),
    "event_data_strategy": (
        "a strategy built from application_sdk.handler.contracts.CloudEventEnvelope",
        "it generates the v2 server's Dapr event payload",
    ),
}


def _deprecated_constant_value(name: str) -> object:
    if name == "auth_credentials_strategy":
        return _auth_credentials_strategy
    if name == "metadata_strategy":
        return _metadata_strategy
    if name == "payload_strategy":
        return _payload_strategy
    if name == "workflow_event_strategy":
        return _workflow_event_strategy
    if name == "event_data_strategy":
        return _event_data_strategy
    raise AssertionError(name)


def __getattr__(name: str) -> object:
    """Serve the deprecated strategies with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v4.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _deprecated_constant_value(name)
