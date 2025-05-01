from typing import Any, Dict

from hypothesis import strategies as st
from hypothesis.strategies import composite

# Strategy for generating safe string values
safe_string_strategy = st.text()

# Strategy for generating credential values
credential_value_strategy = st.one_of(
    safe_string_strategy,
    st.integers(),
    st.booleans(),
    st.none(),
)

# Strategy for common credential keys
common_credential_keys = st.sampled_from(
    [
        "username",
        "password",
        "host",
        "port",
        "database",
        "schema",
        "warehouse",
        "role",
        "account",
        "token",
        "api_key",
        "secret_key",
        "access_key",
        "region",
        "cluster",
        "project",
        "organization",
    ]
)

# Strategy for generating UUIDs
uuid_strategy = st.uuids().map(str)


@composite
def credentials_strategy(draw) -> Dict[str, Any]:
    """Generate a dictionary of credentials with common keys."""
    # Always include username and password as they're most common
    num_fields = draw(st.integers(min_value=2))
    required_keys = ["username", "password"]
    optional_keys = draw(
        st.lists(
            common_credential_keys,
            min_size=num_fields - 2,
            max_size=num_fields - 2,
            unique=True,
        )
    )

    credentials = {
        key: draw(credential_value_strategy) for key in required_keys + optional_keys
    }
    return credentials


@composite
def configuration_strategy(draw) -> Dict[str, Any]:
    """Generate a configuration dictionary that might include nested structures."""
    # Generate base configuration with credentials
    config = draw(credentials_strategy())

    # Add some common configuration fields
    extra_fields = {
        "connection_timeout": draw(st.integers()),
        "max_retries": draw(st.integers()),
        "batch_size": draw(st.integers()),
        "is_secure": draw(st.booleans()),
        "debug_mode": draw(st.booleans()),
        "environment": draw(st.sampled_from(["dev", "staging", "prod"])),
    }

    # Optionally add nested configuration
    if draw(st.booleans()):
        extra_fields["advanced_settings"] = {
            "pool_size": draw(st.integers()),
            "retry_interval": draw(st.integers()),
            "timeout_policy": draw(st.sampled_from(["strict", "lenient", "adaptive"])),
        }

    config.update(extra_fields)
    return config


# Strategy for generating state store keys
state_store_key_strategy = st.builds(
    lambda prefix, uuid: f"{prefix}_{uuid}",
    prefix=st.sampled_from(["credential", "config"]),
    uuid=uuid_strategy,
)

# Strategy for generating complete state store entries
state_store_entry_strategy = st.builds(
    lambda key, value: {"key": key, "value": value},
    key=state_store_key_strategy,
    value=st.one_of(credentials_strategy(), configuration_strategy()),
)
