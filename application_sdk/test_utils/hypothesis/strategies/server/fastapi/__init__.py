import json

from hypothesis import strategies as st

from application_sdk.outputs.eventstore import Event

# Strategy for generating auth credentials
auth_credentials_strategy = st.fixed_dictionaries(
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
metadata_strategy = st.fixed_dictionaries(
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
payload_strategy = st.fixed_dictionaries(
    {
        "credentials": auth_credentials_strategy,
        "metadata": metadata_strategy,
    }
)

# Strategy for generating workflow events
workflow_event_strategy = st.builds(
    Event,
    event_type=st.just("application_event"),
    event_name=st.just("workflow_start"),
    data={},
)

# Strategy for generating complete event data
event_data_strategy = st.fixed_dictionaries(
    {
        "data": workflow_event_strategy,
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
