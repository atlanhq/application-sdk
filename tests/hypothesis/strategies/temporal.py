from typing import Any, Dict

from hypothesis import strategies as st

# Strategy for generating temporal client configuration
temporal_config_strategy = st.fixed_dictionaries(
    {
        "host": st.one_of(
            st.just("localhost"),
            st.from_regex(r"[a-z0-9-]+\.[a-z0-9-]+\.[a-z]{2,}").map(str),
        ),
        "port": st.integers(min_value=1024, max_value=65535).map(str),
        "application_name": st.text(
            min_size=3,
            max_size=50,
            alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd", "_", "-")),
        ),
        "namespace": st.one_of(
            st.just("default"),
            st.text(
                min_size=3,
                max_size=20,
                alphabet=st.characters(
                    whitelist_categories=("Ll", "Lu", "Nd", "_", "-")
                ),
            ),
        ),
    }
)

# Strategy for generating workflow credentials
workflow_credentials_strategy = st.fixed_dictionaries(
    {
        "username": st.text(min_size=3, max_size=50),
        "password": st.text(min_size=8, max_size=100),
        "api_key": st.text(min_size=32, max_size=64),
        "token": st.text(min_size=32, max_size=256),
        "expires_at": st.datetimes().map(lambda dt: dt.isoformat() + "Z"),
    }
)

# Strategy for generating workflow arguments
workflow_args_strategy = st.fixed_dictionaries(
    {
        "credentials": workflow_credentials_strategy,
        "workflow_id": st.uuids().map(str),
        "task_queue": st.text(min_size=3, max_size=50),
        "retry_policy": st.fixed_dictionaries(
            {
                "initial_interval": st.integers(min_value=1, max_value=60),
                "backoff_coefficient": st.floats(min_value=1.0, max_value=2.0),
                "maximum_interval": st.integers(min_value=60, max_value=300),
                "maximum_attempts": st.integers(min_value=1, max_value=10),
            }
        ),
        "metadata": st.dictionaries(
            keys=st.text(min_size=1, max_size=50),
            values=st.one_of(
                st.text(), st.integers(), st.booleans(), st.lists(st.text())
            ),
            min_size=1,
            max_size=10,
        ),
    }
)

# Strategy for generating workflow execution results
workflow_result_strategy = st.fixed_dictionaries(
    {
        "workflow_id": st.uuids().map(str),
        "run_id": st.uuids().map(str),
        "task_queue": st.text(min_size=3, max_size=50),
        "workflow_type": st.text(min_size=3, max_size=50),
        "start_time": st.datetimes().map(lambda dt: dt.isoformat() + "Z"),
        "close_time": st.datetimes().map(lambda dt: dt.isoformat() + "Z"),
        "status": st.sampled_from(
            [
                "COMPLETED",
                "FAILED",
                "CANCELED",
                "TERMINATED",
                "CONTINUED_AS_NEW",
                "TIMED_OUT",
            ]
        ),
        "history_length": st.integers(min_value=1, max_value=1000),
    }
)

# Strategy for generating activity configurations
activity_config_strategy = st.fixed_dictionaries(
    {
        "activity_id": st.uuids().map(str),
        "activity_type": st.text(min_size=3, max_size=50),
        "task_queue": st.text(min_size=3, max_size=50),
        "schedule_to_close_timeout": st.integers(min_value=1, max_value=3600),
        "schedule_to_start_timeout": st.integers(min_value=1, max_value=300),
        "start_to_close_timeout": st.integers(min_value=1, max_value=3600),
        "heartbeat_timeout": st.integers(min_value=0, max_value=60),
        "retry_policy": st.fixed_dictionaries(
            {
                "initial_interval": st.integers(min_value=1, max_value=60),
                "maximum_interval": st.integers(min_value=60, max_value=300),
                "maximum_attempts": st.integers(min_value=0, max_value=10),
                "non_retryable_error_types": st.lists(st.text(min_size=1), max_size=5),
            }
        ),
    }
)

# Strategy for generating worker configurations
worker_config_strategy = st.fixed_dictionaries(
    {
        "max_concurrent_activities": st.integers(min_value=1, max_value=100),
        "max_concurrent_workflows": st.integers(min_value=1, max_value=100),
        "max_cached_workflows": st.integers(min_value=1, max_value=1000),
        "sticky_queue_schedule_to_start_timeout": st.integers(
            min_value=1, max_value=60
        ),
        "max_heartbeat_throttle_interval": st.integers(min_value=1, max_value=60),
        "default_heartbeat_throttle_interval": st.integers(min_value=1, max_value=30),
        "max_activities_per_second": st.integers(min_value=1, max_value=1000),
        "max_task_queue_activities_per_second": st.integers(
            min_value=1, max_value=1000
        ),
        "graceful_shutdown_timeout": st.integers(min_value=1, max_value=300),
    }
)
