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

from application_sdk.testing.harness.spec import AppUnderTest

__all__ = ["AppConfig"]

#: Version that removes :class:`AppConfig`. Every deprecation in this SDK names
#: its removal version; this is the one for that class.
#:
#: The warning text below spells "v4.0" literally rather than interpolating this
#: constant. Conformance rule B002 reads the notice as written in the source, so
#: an f-string placeholder makes a compliant deprecation look like one that never
#: named its removal version. ``test_deprecation_names_a_removal_version`` pins
#: the two together so they cannot drift.
APP_CONFIG_REMOVAL_VERSION = "4.0"


class AppConfig(AppUnderTest):
    """Deprecated (removed in v4.0) — use :class:`AppUnderTest`.

    A subclass, so a downstream fixture already annotated ``-> AppUnderTest``
    accepts an ``AppConfig`` unchanged for the whole migration window.

    **The positional signature is preserved exactly**, which is why this declares
    an explicit ``__init__`` instead of inheriting a generated one. Field order
    on :class:`AppUnderTest` is ``(app_name, namespace, handler_port)``, so a
    dataclass-generated subclass ``__init__`` would have bound
    ``AppConfig("app", "module", "ns", "image")`` as ``namespace="module"`` and
    ``handler_port="ns"`` — accepted silently, wrong at every read. A shim that
    mis-binds its arguments is a silent break, not a deprecation.

    Four of the seven fields were never read by anything — not by this package,
    not by ``tests/e2e/conftest.py``'s fixtures, not by any consumer repo — so
    they are accepted and stored but carry no meaning. They are absent from
    :class:`AppUnderTest`.

    Two other compatibility details, both deliberate:

    * **Mutability is preserved.** :class:`AppUnderTest` is frozen; the original
      ``AppConfig`` was a plain mutable dataclass, so ``cfg.timeout = 600``
      worked. Freezing it here would be a second silent break in the same shim,
      so ``__setattr__`` and ``__delattr__`` are restored.
    * ``__eq__`` and ``__repr__`` come from :class:`AppUnderTest` and therefore
      consider only the three live fields. Two ``AppConfig`` instances differing
      only in ``image`` compare equal — correct, given ``image`` has no effect on
      anything.

    Args:
        app_name: Kubernetes resource prefix.
        app_module: Ignored. Nothing ever read it; the deployer that would have
            used it was never shipped.
        namespace: Kubernetes namespace the app is deployed into.
        image: Ignored, same reason as ``app_module``.
        handler_port: Port the handler Service listens on.
        worker_health_port: Ignored. The worker health probe reads
            ``BaseE2ETest.worker_health_url``, which carries its own port.
        timeout: Ignored. Waits take a
            :class:`~application_sdk.testing.harness.Budget`.
    """

    __slots__ = ("app_module", "image", "timeout", "worker_health_port")

    def __init__(
        self,
        app_name: str = "",
        app_module: str = "",
        namespace: str = "",
        image: str = "",
        handler_port: int = 8000,
        worker_health_port: int = 8081,
        timeout: int = 300,
    ) -> None:
        """Construct the shim, warning that it is deprecated.

        Positional order is the original's, verbatim. The four originally
        required fields now default to empty rather than raising, because a
        deprecation shim should accept everything the old signature accepted and
        a little more — never less.
        """
        warnings.warn(
            "application_sdk.testing.e2e.AppConfig is deprecated and will be "
            "removed in v4.0; use "
            "application_sdk.testing.harness.AppUnderTest instead. It collides "
            "by name with application_sdk.main.AppConfig, the runtime config "
            "object. app_module, image, worker_health_port and timeout are not "
            "carried over — nothing ever read them.",
            DeprecationWarning,
            stacklevel=2,
        )
        # AppUnderTest is frozen, so its generated __init__ is the only way in.
        super().__init__(
            app_name=app_name, namespace=namespace, handler_port=handler_port
        )
        object.__setattr__(self, "app_module", app_module)
        object.__setattr__(self, "image", image)
        object.__setattr__(self, "worker_health_port", worker_health_port)
        object.__setattr__(self, "timeout", timeout)

    def __setattr__(self, name: str, value: object) -> None:
        """Restore the original's mutability — see the class docstring."""
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        """Restore the original's mutability — see the class docstring."""
        object.__delattr__(self, name)
