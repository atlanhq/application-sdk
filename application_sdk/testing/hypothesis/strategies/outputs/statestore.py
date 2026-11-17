"""Hypothesis strategies for state-store entries (credentials and configs).

.. deprecated:: 3.59.0
    Every strategy here is deprecated and removed in v4.0.0. Nothing in the
    SDK imports them and no test exercises them; build strategies from your
    app's own credential and config models in its tests instead.
"""

import warnings
from typing import Any, Dict

from hypothesis import strategies as st
from hypothesis.strategies import composite
from typing_extensions import deprecated

#: Upper bound for config integers (timeouts, retries, sizes).
_MAX_CONFIG_INT = 86_400

# Strategy for generating safe string values
_safe_string_strategy = st.text()

# Strategy for generating credential values
_credential_value_strategy = st.one_of(
    _safe_string_strategy,
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.booleans(),
    st.none(),
)

_CREDENTIAL_KEYS = [
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

# Strategy for common credential keys
_common_credential_keys = st.sampled_from(_CREDENTIAL_KEYS)

# Strategy for generating UUIDs
_uuid_strategy = st.uuids().map(str)

_config_int = st.integers(min_value=0, max_value=_MAX_CONFIG_INT)


@composite
def _credentials_strategy(draw) -> Dict[str, Any]:
    """Generate a dictionary of credentials with common keys."""
    # Always include username and password as they're most common. The
    # optional keys are drawn unique from the 17 above, so at most 17 of them.
    num_optional = draw(st.integers(min_value=0, max_value=len(_CREDENTIAL_KEYS)))
    required_keys = ["username", "password"]
    optional_keys = draw(
        st.lists(
            _common_credential_keys,
            min_size=num_optional,
            max_size=num_optional,
            unique=True,
        )
    )

    credentials = {
        key: draw(_credential_value_strategy) for key in required_keys + optional_keys
    }
    return credentials


@composite
def _configuration_strategy(draw) -> Dict[str, Any]:
    """Generate a configuration dictionary that might include nested structures."""
    # Generate base configuration with credentials
    config = draw(_credentials_strategy())

    # Add some common configuration fields
    extra_fields = {
        "connection_timeout": draw(_config_int),
        "max_retries": draw(_config_int),
        "batch_size": draw(_config_int),
        "is_secure": draw(st.booleans()),
        "debug_mode": draw(st.booleans()),
        "environment": draw(st.sampled_from(["dev", "staging", "prod"])),
    }

    # Optionally add nested configuration
    if draw(st.booleans()):
        extra_fields["advanced_settings"] = {
            "pool_size": draw(_config_int),
            "retry_interval": draw(_config_int),
            "timeout_policy": draw(st.sampled_from(["strict", "lenient", "adaptive"])),
        }

    config.update(extra_fields)
    return config


@deprecated(
    "credentials_strategy is deprecated; use a strategy built from your app's "
    "own credential model instead — no SDK code or test uses this module. "
    "Will be removed in v4.0.0."
)
@composite
def credentials_strategy(draw) -> Dict[str, Any]:
    """Generate a dictionary of credentials with common keys.

    .. deprecated:: 3.59.0
        Build a strategy from your app's own credential model. Will be removed
        in v4.0.0.
    """
    return draw(_credentials_strategy())


@deprecated(
    "configuration_strategy is deprecated; use a strategy built from your app's "
    "own config model instead — no SDK code or test uses this module. "
    "Will be removed in v4.0.0."
)
@composite
def configuration_strategy(draw) -> Dict[str, Any]:
    """Generate a configuration dictionary that might include nested structures.

    .. deprecated:: 3.59.0
        Build a strategy from your app's own config model. Will be removed in
        v4.0.0.
    """
    return draw(_configuration_strategy())


# Strategy for generating state store keys
_state_store_key_strategy = st.builds(
    lambda prefix, uuid: f"{prefix}_{uuid}",
    prefix=st.sampled_from(["credential", "config"]),
    uuid=_uuid_strategy,
)

# Strategy for generating complete state store entries
_state_store_entry_strategy = st.builds(
    lambda key, value: {"key": key, "value": value},
    key=_state_store_key_strategy,
    value=st.one_of(_credentials_strategy(), _configuration_strategy()),
)


#: name -> (replacement, why). Served by __getattr__ below (PEP 562).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "safe_string_strategy": (
        "hypothesis.strategies.text()",
        "no SDK code or test uses this module",
    ),
    "credential_value_strategy": (
        "a strategy built from your app's own credential model",
        "no SDK code or test uses this module",
    ),
    "common_credential_keys": (
        "a strategy built from your app's own credential model",
        "no SDK code or test uses this module",
    ),
    "uuid_strategy": (
        "hypothesis.strategies.uuids().map(str)",
        "no SDK code or test uses this module",
    ),
    "state_store_key_strategy": (
        "a strategy built from your app's own state-store keys",
        "no SDK code or test uses this module",
    ),
    "state_store_entry_strategy": (
        "a strategy built from your app's own credential and config models",
        "no SDK code or test uses this module",
    ),
}

#: A star import reads ``__all__`` and resolves each name through ``__getattr__``,
#: so ``import *`` keeps binding every old name, with the warning. Without it,
#: Python would enumerate only the module's real globals and skip them all.
__all__ = [*_DEPRECATED_CONSTANTS, "credentials_strategy", "configuration_strategy"]


def _deprecated_constant_value(name: str) -> object:
    return globals()[f"_{name}"]


def __getattr__(name: str) -> object:
    """Serve the deprecated strategies with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _deprecated_constant_value(name)
