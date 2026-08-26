"""Deprecated home of ``AppConfig``. Use ``harness.AppUnderTest``.

``AppConfig`` here collided by name with :class:`application_sdk.main.AppConfig`,
which is the authoritative *runtime* config object every app is configured
through. Two unrelated types sharing a name was tolerable while this one was
unexported plumbing; it stopped being tolerable at the point the name was about
to become shared vocabulary across the harness, the runtime scenario suite and
the connector suites.

The replacement is :class:`application_sdk.testing.harness.AppUnderTest`.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

from application_sdk.testing.harness.spec import AppUnderTest

__all__ = ["AppConfig"]

#: Version that removes :class:`AppConfig`. Every deprecation in this SDK names
#: its removal version; this is the one for that class.
APP_CONFIG_REMOVAL_VERSION = "4.0"


@dataclass(frozen=True, slots=True)
class AppConfig(AppUnderTest):
    """Deprecated (removed in v4.0) — use :class:`AppUnderTest`.

    Kept so an existing ``AppConfig(...)`` call site keeps working. The four
    fields below were never read by anything — not by this package, not by
    ``tests/e2e/conftest.py``'s fixtures, not by any consumer repo — so they are
    accepted and ignored rather than carried into
    :class:`~application_sdk.testing.harness.AppUnderTest`.

    ``AppUnderTest`` also reorders the fields (``app_name``, ``namespace``,
    ``handler_port``), so construct this by keyword. Both known call sites
    already do.

    Attributes:
        app_module: Ignored. Nothing ever read it; a deployer that would have
            used it was never shipped.
        image: Ignored, same reason.
        worker_health_port: Ignored. The worker health probe reads
            ``BaseE2ETest.worker_health_url``, which carries its own port.
        timeout: Ignored. Waits take a
            :class:`~application_sdk.testing.harness.Budget`.
    """

    app_module: str = ""
    image: str = ""
    worker_health_port: int = 8081
    timeout: int = 300

    def __post_init__(self) -> None:
        """Warn that this class is deprecated, naming its replacement."""
        warnings.warn(
            "application_sdk.testing.e2e.AppConfig is deprecated and will be "
            f"removed in v{APP_CONFIG_REMOVAL_VERSION}; use "
            "application_sdk.testing.harness.AppUnderTest instead. It collides "
            "by name with application_sdk.main.AppConfig, the runtime config "
            "object. app_module, image, worker_health_port and timeout are not "
            "carried over — nothing ever read them.",
            DeprecationWarning,
            stacklevel=2,
        )
