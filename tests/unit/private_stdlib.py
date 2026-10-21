"""Patch a stdlib function for one SDK module without patching it for the process.

SDK modules do ``import time`` / ``import asyncio`` / ``import os``, so
``patch("application_sdk.X.time.monotonic")`` resolves to the one global
``time`` module and patches ``time.monotonic`` for every caller in the xdist
worker: ``loop.time()``, ``asyncio.timeout``, ``wait_for``, and every thread an
earlier test left running (a ``run_in_thread`` offload, the loguru flush). A
fake clock that advances 6s per call makes those callers time out at random;
an ``AsyncMock`` ``asyncio.sleep`` turns every library retry into a spin.

:func:`private_stdlib` gives the SDK module its own copy of the stdlib module
for the test, so the patch lands on the copy and only that module sees it::

    with private_stdlib(http_mod, "time") as local_time:
        monkeypatch.setattr(local_time, "monotonic", fake_monotonic)

The copy is a fresh module object holding the same attributes as the real one,
so every function the test does not patch is the real function.
``tests/unit/test_no_global_stdlib_patches.py`` fails a test that patches the
global module through an SDK path instead.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType
from unittest.mock import patch


@contextmanager
def private_stdlib(owner: ModuleType | str, name: str) -> Iterator[ModuleType]:
    """Rebind *owner*'s global *name* to a private copy of that stdlib module.

    Args:
        owner: The SDK module (or its dotted import path) whose reference is
            rebound.
        name: The global name the SDK module imported the stdlib module under,
            such as ``"time"`` or ``"asyncio"``.

    Yields:
        The private copy. Patch attributes on it; the real module is untouched.
    """
    module = importlib.import_module(owner) if isinstance(owner, str) else owner
    real = getattr(module, name)
    if not isinstance(real, ModuleType):
        raise TypeError(f"{module.__name__}.{name} is not a module: {real!r}")
    copy = ModuleType(real.__name__, real.__doc__)
    copy.__dict__.update(vars(real))
    with patch.object(module, name, copy):
        yield copy
