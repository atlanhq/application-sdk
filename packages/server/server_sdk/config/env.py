"""Per-app environment reader for a shared process (ARUN-942).

One host process serves several apps, so a bare ``os.environ`` read cannot
answer "what is *my* value" — there is no correct process-global answer when
ai-memory wants ``EMBEDDING_MODEL=A`` and another app wants ``B``. Each app's
own config is therefore published under a per-app prefix, and this reader tries
that prefix before the bare name.

    env = AppEnv("ai-memory")
    env.get("EMBEDDING_MODEL", "text-embedding-3-small")
    # -> ATLAN_APPENV__AI_MEMORY__EMBEDDING_MODEL, else EMBEDDING_MODEL

The bare-name fallback is what keeps this cheap to adopt: values that really are
process-wide (S3, Temporal, logging, the tenant's Keycloak service account) stay
unprefixed and need no per-app copy, and an app that has not been ported yet
keeps working unchanged.

Reads hit ``os.environ`` on every call rather than caching. That is deliberate:
a cached reader would freeze whatever the environment happened to be at import,
which is exactly the import-time-versus-request-time trap this class exists to
remove. The reader behaves identically wherever it is called.
"""

from __future__ import annotations

import os
from typing import Final

__all__ = ["APPENV_PREFIX", "AppEnv", "prefix_for"]

APPENV_PREFIX: Final = "ATLAN_APPENV__"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def prefix_for(app_name: str) -> str:
    """The env prefix for ``app_name`` — e.g. ``ai-memory`` -> ``ATLAN_APPENV__AI_MEMORY__``.

    CI writes variables under this prefix and the reader consumes them, so the
    naming rule has exactly one definition. Anything generating these names
    should call this rather than re-implement the transform.
    """
    if not app_name or not app_name.strip():
        raise ValueError("app_name must be a non-empty string")
    normalised = app_name.strip().upper().replace("-", "_").replace(".", "_")
    return f"{APPENV_PREFIX}{normalised}__"


class AppEnv:
    """Reads environment values scoped to one app, falling back to the bare name."""

    __slots__ = ("app_name", "_prefix")

    def __init__(self, app_name: str) -> None:
        self.app_name = app_name
        self._prefix = prefix_for(app_name)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AppEnv({self.app_name!r})"

    @property
    def prefix(self) -> str:
        return self._prefix

    def get(self, key: str, default: str | None = None) -> str | None:
        """Prefixed value if set, else the bare value, else ``default``.

        An empty string counts as set. Apps use ``FOO=""`` to mean "explicitly
        blank" (server.py:245 treats a blank contract dir that way), so
        collapsing it to unset would change behaviour.
        """
        scoped = os.environ.get(self._prefix + key)
        if scoped is not None:
            return scoped
        return os.environ.get(key, default)

    def require(self, key: str) -> str:
        """Like :meth:`get`, but raises when the value is missing or blank.

        For config whose absence should stop the app rather than surface later as
        a confusing downstream failure.
        """
        value = self.get(key)
        if value is None or not value.strip():
            raise KeyError(
                f"{key!r} is not set for app {self.app_name!r} "
                f"(looked for {self._prefix + key!r}, then {key!r})"
            )
        return value

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Parse a boolean. Unrecognised values fall back to ``default``.

        Lenient on purpose: a typo'd flag should not crash a hosted app at
        import and take the mount with it.
        """
        raw = self.get(key)
        if raw is None:
            return default
        value = raw.strip().lower()
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        return default

    def get_int(self, key: str, default: int) -> int:
        """Parse an int, falling back to ``default`` when unset or unparseable."""
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return int(raw.strip())
        except ValueError:
            return default
