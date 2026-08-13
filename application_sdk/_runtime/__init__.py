"""Dependency-neutral runtime substrate — the SDK's bottom layer (ADR-0019).

Everything in this package may be imported **at module scope from anywhere in
the SDK**, including from ``storage/``. That is the package's entire purpose:
the offload pool and the progress seam are needed by every layer, so they cannot
live in a layer.

**The rule for modules in here.** A ``_runtime`` module may import from the
standard library, from third-party packages, and from
``application_sdk.observability.logger_adaptor``. Nothing else in
``application_sdk``. In particular, never ``application_sdk.contracts``,
``application_sdk.credentials``, ``application_sdk.storage``,
``application_sdk.execution`` or ``application_sdk.app`` — each of those pulls
in ``storage.ops`` transitively, which is exactly the cycle this package exists
to remove. ``tests/unit/runtime/test_layering.py`` enforces this in a
subprocess, so breaking the rule fails CI rather than surfacing as an
``ImportError`` at some unrelated call site months later.

**Private on purpose.** The app-facing paths are unchanged and stay where the
docs point them: ``application_sdk.execution.heartbeat`` for the offload
primitives and ``application_sdk.execution.progress`` for the progress seam,
both thin re-exports over this package. SDK-internal code imports from here
directly; apps should not need to know this package exists.
"""
