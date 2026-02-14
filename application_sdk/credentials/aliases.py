"""Field alias resolution for credential compatibility.

Different services use different names for the same concept:
- Stripe calls it "secret_key"
- OpenAI calls it "api_key"
- Some call it "token" or "access_token"

This module provides alias resolution so credentials work regardless
of the field names used by the original provider.
"""

from typing import Any, Dict, List

# Standard field aliases - all resolve to the canonical field name (first in list)
# Format: canonical_name -> [aliases]
FIELD_ALIASES: Dict[str, List[str]] = {
    # API key variations
    "api_key": ["api_token", "token", "secret_key", "access_token", "key", "apikey"],
    # Identity variations
    "username": ["user", "login", "account", "user_id", "userid"],
    "password": ["pass", "pwd", "secret"],
    "email": ["email_address", "mail", "user_email"],
    # OAuth variations
    "client_id": ["app_id", "application_id", "consumer_key", "clientid"],
    "client_secret": [
        "app_secret",
        "application_secret",
        "consumer_secret",
        "clientsecret",
    ],
    "access_token": ["token", "bearer_token", "oauth_token"],
    "refresh_token": ["refresh", "oauth_refresh_token"],
    # AWS variations
    "access_key_id": ["aws_access_key_id", "access_key", "accesskeyid"],
    "secret_access_key": ["aws_secret_access_key", "secret_key", "secretaccesskey"],
    "session_token": ["aws_session_token", "security_token"],
    "region": ["aws_region", "region_name"],
    # Database variations
    "database": ["db", "dbname", "schema", "database_name", "catalog"],
    "host": ["hostname", "server", "endpoint", "host_name"],
    "port": ["port_number"],
    # Certificate variations
    "client_cert": ["certificate", "cert", "client_certificate"],
    "client_key": ["private_key", "key", "client_private_key"],
    "ca_cert": ["ca_certificate", "root_cert", "ca"],
}

# Build reverse lookup: alias -> canonical
_ALIAS_TO_CANONICAL: Dict[str, str] = {}
for canonical, aliases in FIELD_ALIASES.items():
    for alias in aliases:
        _ALIAS_TO_CANONICAL[alias.lower()] = canonical


def resolve_field_aliases(credentials: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve field aliases to canonical field names.

    This function takes a credentials dict with potentially aliased field names
    and returns a new dict with canonical names. Original field names are
    preserved alongside canonical names for backwards compatibility.

    Args:
        credentials: Raw credentials dict with potentially aliased field names.

    Returns:
        Dict with canonical field names added (original names preserved).

    Example:
        >>> creds = {"secret_key": "sk_live_xxx", "host": "api.stripe.com"}
        >>> resolved = resolve_field_aliases(creds)
        >>> resolved["api_key"]  # Canonical name
        'sk_live_xxx'
        >>> resolved["secret_key"]  # Original preserved
        'sk_live_xxx'
    """
    resolved: Dict[str, Any] = {}

    for key, value in credentials.items():
        # Always keep the original
        resolved[key] = value

        # Check if this is an alias and add canonical name
        canonical = _ALIAS_TO_CANONICAL.get(key.lower())
        if canonical and canonical not in resolved:
            resolved[canonical] = value

    return resolved


def get_canonical_field_name(field_name: str) -> str:
    """Get the canonical name for a field.

    If the field name is an alias, returns the canonical name.
    Otherwise, returns the original field name.

    Args:
        field_name: Potentially aliased field name.

    Returns:
        Canonical field name.

    Example:
        >>> get_canonical_field_name("secret_key")
        'api_key'
        >>> get_canonical_field_name("username")
        'username'
        >>> get_canonical_field_name("custom_field")
        'custom_field'
    """
    return _ALIAS_TO_CANONICAL.get(field_name.lower(), field_name)


def get_field_value(
    credentials: Dict[str, Any], canonical_name: str, default: Any = None
) -> Any:
    """Get a field value using canonical name with alias fallback.

    Tries to find the value using the canonical name first,
    then falls back to checking all aliases.

    Args:
        credentials: Credentials dictionary.
        canonical_name: Canonical field name to look up.
        default: Default value if not found.

    Returns:
        Field value or default.

    Example:
        >>> creds = {"secret_key": "sk_xxx"}
        >>> get_field_value(creds, "api_key")
        'sk_xxx'
    """
    # Try canonical name first
    if canonical_name in credentials:
        return credentials[canonical_name]

    # Try aliases
    aliases = FIELD_ALIASES.get(canonical_name, [])
    for alias in aliases:
        if alias in credentials:
            return credentials[alias]
        # Try case-insensitive
        for key in credentials:
            if key.lower() == alias.lower():
                return credentials[key]

    return default
