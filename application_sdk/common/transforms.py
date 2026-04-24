"""Credential format transformation utilities.

.. deprecated::
    These utilities exist for backward compatibility during the v2 → v3
    migration. They normalize the wire format (hyphenated/dotted keys from
    the frontend and Argo templates) into the format connectors and the
    vault expect. Once all apps are fully native and the frontend sends
    the canonical format directly, these transformations should be removed.
"""

from __future__ import annotations

from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


# -------------------------------------------------------------------------
# Key format converters
# -------------------------------------------------------------------------


def kebab_to_camel(key: str) -> str:
    """Convert a kebab-case key to camelCase.

    Example::

        kebab_to_camel("auth-type")      → "authType"
        kebab_to_camel("aws-region")     → "awsRegion"
        kebab_to_camel("host")           → "host"  (no-op)
    """
    if "-" not in key:
        return key
    parts = key.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def camel_to_kebab(key: str) -> str:
    """Convert a camelCase key to kebab-case.

    Example::

        camel_to_kebab("authType")       → "auth-type"
        camel_to_kebab("awsRegion")      → "aws-region"
        camel_to_kebab("host")           → "host"  (no-op)
    """
    result: list[str] = []
    for char in key:
        if char.isupper():
            result.append("-")
            result.append(char.lower())
        else:
            result.append(char)
    return "".join(result)


# -------------------------------------------------------------------------
# Dotted key expansion
# -------------------------------------------------------------------------


def expand_dotted_keys(flat: dict[str, Any]) -> dict[str, Any]:
    """Expand dotted keys into nested dicts.

    ``{"extra.database": "db", "basic.username": "u", "host": "h"}``
    becomes
    ``{"extra": {"database": "db"}, "basic": {"username": "u"}, "host": "h"}``

    Keys without dots are copied as-is. If a dotted key's parent
    conflicts with a non-dict value at that root, the existing value
    wins and the dotted key is dropped with a debug log.
    """
    out: dict[str, Any] = {}

    # Pass 1: non-dotted keys first.
    for key, value in flat.items():
        if "." not in key:
            out[key] = value

    # Pass 2: dotted keys, merging into existing dicts.
    for key, value in flat.items():
        if "." not in key:
            continue
        parts = key.split(".")
        cursor: Any = out
        ok = True
        for part in parts[:-1]:
            existing = cursor.get(part) if isinstance(cursor, dict) else None
            if existing is None:
                cursor[part] = {}
                cursor = cursor[part]
            elif isinstance(existing, dict):
                cursor = existing
            else:
                logger.debug(
                    "Dotted key %r conflicts with non-dict root %r=%r; skipping",
                    key,
                    part,
                    existing,
                )
                ok = False
                break
        if ok:
            cursor[parts[-1]] = value

    return out


# -------------------------------------------------------------------------
# Auth section flattening
# -------------------------------------------------------------------------


def flatten_auth_section(creds: dict[str, Any]) -> dict[str, Any]:
    """Promote the auth-type section to root level for client compatibility.

    Reads ``auth-type`` (e.g. ``"basic"``), finds the matching nested
    dict, and deep-merges its contents to root — so existing root-level
    dicts (e.g. ``extra``) are merged rather than overwritten.

    Example::

        {"auth-type": "gcp-wif",
         "extra": {"connect_type": "public"},
         "gcp-wif": {"extra": {"project_id": "p"}}}
        →
        {"auth-type": "gcp-wif",
         "extra": {"connect_type": "public", "project_id": "p"},
         "gcp-wif": {"extra": {"project_id": "p"}}}
    """
    auth_type = creds.get("auth-type", "")
    if not auth_type:
        return creds
    auth_section = creds.get(auth_type)
    if not isinstance(auth_section, dict):
        return creds
    for key, value in auth_section.items():
        existing = creds.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            existing.update(value)
        else:
            creds[key] = value
    return creds


# -------------------------------------------------------------------------
# Full agent credential transformation
# -------------------------------------------------------------------------


def transform_agent_credentials(
    agent_json: dict[str, Any],
) -> dict[str, Any]:
    """Transform raw agent JSON into the format connectors expect.

    1. ``auth-type`` → ``authType``
    2. Adds ``credentialSource = "agent"``
    3. Expands ``extra.field`` → ``{"extra": {"field": value}}``
    4. Expands ``{authType}.field`` → root-level ``field``
    5. Expands ``{authType}.extra.field`` → ``{"extra": {"field": value}}``

    Args:
        agent_json: Raw agent credential dict (wire format from frontend).

    Returns:
        Transformed credential dict matching the vault/direct-flow format.
    """
    creds = agent_json.copy()

    # Step 1: auth-type → authType
    if "auth-type" in creds:
        creds["authType"] = creds.pop("auth-type")

    # Step 2: mark as agent source
    creds["credentialSource"] = "agent"

    # Step 4: expand dotted keys (extra.field, {authType}.field, etc.)
    auth_type = creds.get("authType", "")
    auth_prefix = f"{auth_type}." if auth_type else ""
    extra_prefix = f"{auth_type}.extra." if auth_type else ""

    fields_to_remove: list[str] = []
    extra_fields: dict[str, Any] = {}

    # Root-level extra.field → extra: {field: value}
    for key, value in agent_json.items():
        if key.startswith("extra."):
            field_name = key[len("extra."):]
            extra_fields[field_name] = value
            fields_to_remove.append(key)

    # {authType}.extra.field → extra: {field: value} (overrides root extra)
    # {authType}.field → root-level field
    for key, value in agent_json.items():
        if extra_prefix and key.startswith(extra_prefix):
            field_name = key[len(extra_prefix):]
            extra_fields[field_name] = value
            fields_to_remove.append(key)
        elif auth_prefix and key.startswith(auth_prefix):
            field_name = key[len(auth_prefix):]
            creds[field_name] = value
            fields_to_remove.append(key)

    for key in fields_to_remove:
        creds.pop(key, None)

    if extra_fields:
        creds["extra"] = extra_fields

    return creds
