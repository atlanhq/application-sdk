"""Credential-name heuristic shared by the S-series detectors (S001, S002).

The S-series owns this predicate rather than reusing the L-series
``CREDENTIAL_VALUE_SUFFIXES``: the env-var-name surface S002 inspects
(SCREAMING_SNAKE, ``AWS_``/``AZURE_`` prefixes, ``..._ACCESS_KEY`` forms) genuinely
diverges from the log-argument surface L010 targets — the L list omits
``access_key``/``secret_access_key`` and would miss real reads like
``AWS_SECRET_ACCESS_KEY``.  Keeping a dedicated predicate also avoids a
cross-series private import (``security`` reaching into ``logging._helpers``),
which the check architecture deliberately forbids.

The predicate is name-only and deterministic: it never inspects values, so the
same identifier always classifies the same way.
"""

from __future__ import annotations

# Identifier *tokens* that mark a credential **value** (not a label/reference).
# A name matches when it equals a token or ends with ``"_" + token`` — the
# leading-underscore boundary keeps ``token`` from matching ``broken`` etc.
_VALUE_TOKENS: tuple[str, ...] = (
    "password",
    "passwd",
    "client_secret",
    "secret_access_key",
    "access_key",
    "secret",
    "private_key",
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "bearer_token",
    "bearer",
    "token",
    "credentials",
    "credential",
)

# Suffixes that mark a name as a *label / reference*, not the secret itself —
# e.g. ``token_name``, ``access_key_id``, ``token_url`` (an endpoint), the
# ``SECRET_STORE_NAME`` store-name read.  Checked first, so a label suffix
# always wins over a value token.
_LABEL_SUFFIXES: tuple[str, ...] = (
    "_id",
    "_name",
    "_type",
    "_label",
    "_url",
    "_uri",
    "_path",
    "_arn",
    "_key_name",
)


def is_credential_value_name(name: str) -> bool:
    """True if *name* looks like a credential **value** identifier.

    Matches a credential-value token (``password``, ``api_key``, ``access_key``,
    ``client_secret``, …) as the whole name or a ``_``-bounded suffix, unless the
    name first matches a label suffix (``_id``, ``_name``, ``_url``, …) marking it
    a reference rather than the secret itself.  Case-insensitive, so it covers
    both snake_case identifiers (S001 targets) and SCREAMING_SNAKE env-var names
    (S002 targets).
    """
    lower = name.lower()
    if any(lower.endswith(suffix) for suffix in _LABEL_SUFFIXES):
        return False
    return any(lower == tok or lower.endswith(f"_{tok}") for tok in _VALUE_TOKENS)
