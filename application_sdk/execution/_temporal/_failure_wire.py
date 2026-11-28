"""Translate a typed failure into the ``ApplicationError`` an activity raises.

A leaf of ``execution._temporal``: it imports nothing from ``application_sdk.app``,
so the activity wrapper (``activities``) and the preflight gate
(``preflight_gate``) can share it without the gate reaching the app package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from temporalio.exceptions import FailureError

from application_sdk.errors.base import redact_and_cap
from application_sdk.execution.errors import ApplicationError
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.errors.base import AppError

__all__: list[str] = []

logger = get_logger(__name__)

# Temporal's failure serializer (_error_to_failure) recurses into __cause__ /
# __context__ chains without a depth bound.  A deep or cyclic chain hits
# Python's recursion limit and the serializer's own crash replaces the real
# error with "Failed building exception result: maximum recursion depth
# exceeded" (BLDX-1512).  We sever the chain at this depth before raising
# ApplicationError so the serializer can always complete safely.
#
# Safety maths: serializer uses 2 call frames per level; baseline async call
# stack is ~30–50 frames; at depth 50 we reach ~150 frames total vs. Python's
# default limit of 1000.  Each stack trace is typically 1–5 KB, so 50 levels ≈
# 50–250 KB — well inside Temporal's default 2 MB payload limit.
_MAX_CHAIN_DEPTH = 50


def _sever_cause_chain(exc: BaseException) -> None:
    """Sever the __cause__/__context__ chain at _MAX_CHAIN_DEPTH.

    Walks the chain iteratively (safe against deep stacks) and cuts the link
    once we hit the depth cap or detect a cycle via object identity.  Mutates
    the exception objects in place — safe because this is called immediately
    before we raise a fresh ApplicationError that will own the chain.

    At the cut point both ``__cause__`` and ``__context__`` are nulled out so
    that neither link re-opens the chain.  Any un-traversed alternate link
    (e.g. a ``__context__`` branch not followed because ``__cause__`` was
    taken) is also discarded — this is a deliberate trade-off to guarantee
    termination; the primary causal path up to the depth cap is preserved.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    depth = 0
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        depth += 1
        nxt = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
        if nxt is not None and (depth >= _MAX_CHAIN_DEPTH or id(nxt) in seen):
            current.__cause__ = None
            current.__context__ = None
            break
        current = nxt


def _redacted_cause_chain(exc: BaseException) -> BaseException:
    """Mirror ``exc``'s cause chain as ``ApplicationError``s with redacted messages.

    Raise a translated failure ``from`` this, never ``from`` the original
    exception. Temporal's failure converter serialises every link it walks
    into workflow history as ``str(link)`` verbatim, and many
    ``AppError.__str__`` overrides append ``{type(cause).__name__}: {cause}``
    without redaction — so redacting only the top-level message leaves each
    cause's credentials one level down.

    The mirror has the shape Temporal would record for the original: each link
    keeps its class name as the wire ``type`` and its traceback, and only its
    message changes (redacted and capped by :func:`redact_and_cap`). The walk
    follows the converter's own rule — ``__cause__`` for a plain exception,
    ``__cause__`` then an unsuppressed ``__context__`` for a ``FailureError``
    — stops at ``_MAX_CHAIN_DEPTH`` links, and stops at a revisited link, so a
    cyclic chain terminates.

    Args:
        exc: The head of the chain, usually the exception the activity caught.
            Not mutated.

    Returns:
        The head of the mirrored chain, to pass to ``raise ... from``.
    """
    head: BaseException | None = None
    tail: BaseException | None = None
    seen: set[int] = set()
    current: BaseException | None = exc
    while (
        current is not None and id(current) not in seen and len(seen) < _MAX_CHAIN_DEPTH
    ):
        seen.add(id(current))
        try:
            text = str(current)
        except Exception:  # noqa: BLE001 — a broken __str__ must not replace the real failure
            text = f"<unprintable {type(current).__name__}>"
        mirror = ApplicationError(redact_and_cap(text), type=type(current).__name__)
        mirror.__traceback__ = current.__traceback__
        mirror.__suppress_context__ = True
        if tail is None:
            head = mirror
        else:
            tail.__cause__ = mirror
        tail = mirror
        nxt = current.__cause__
        if (
            nxt is None
            and isinstance(current, FailureError)
            and not current.__suppress_context__
        ):
            nxt = current.__context__
        current = nxt
    assert head is not None  # exc is not None, so the loop ran at least once
    return head


def _to_application_error(e: AppError) -> ApplicationError:
    """Translate a typed :class:`AppError` into the ``ApplicationError`` we raise.

    Shared by every site in this module that fails an activity with a typed
    error — the stall branch of the cancellation handler and the general
    ``AppError`` branch — because all three things it does are easy to get
    subtly different in a second copy, and each one silently degrades a failure
    that a human has to read later:

    - the ``FailureDetails`` guard, so non-serialisable evidence costs the
      structured details rather than replacing the real error with a secondary
      one;
    - ``_sever_cause_chain``, so Temporal's failure serializer can always
      complete (BLDX-1512);
    - ``non_retryable=not e.effective_retryable``, so the error's own retry
      policy reaches Temporal instead of the default;
    - ``redact_and_cap`` on the message, so the free text written to workflow
      history is redacted and length-capped like the ``details`` envelope
      beside it.

    Args:
        e: The typed error to translate. Mutated by the cause-chain severing,
            which is safe because the caller raises the returned
            ``ApplicationError`` immediately.

    Returns:
        The ``ApplicationError`` to raise, with the error's class name as its
        wire ``type``. Callers ``raise ... from _redacted_cause_chain(<caught>)``,
        not ``from <caught>``: the message is redacted here, and the cause
        chain must be too, because Temporal serialises every link of it.
    """
    # Guard FailureDetails construction: evidence fields may contain
    # non-serialisable values; fall back to details-free ApplicationError
    # rather than letting a secondary error mask the original.
    try:
        details: tuple[Any, ...] = (e.to_failure_details(),)
    except Exception:
        logger.warning(
            "Failed to build FailureDetails for %s; raising without structured details",
            type(e).__name__,
            exc_info=True,
        )
        details = ()

    # Sever deep / cyclic __cause__ / __context__ chains before Temporal's
    # failure serializer walks them (BLDX-1512).
    _sever_cause_chain(e)

    # The message sits in workflow history beside the redacted ``details``
    # envelope, so it gets the same treatment: many ``AppError.__str__``
    # overrides append ``{type(cause).__name__}: {cause}`` verbatim, and a
    # driver's cause can carry a connection string. Redact and cap at this one
    # seam rather than in each ``__str__`` copy.
    return ApplicationError(
        redact_and_cap(str(e)),
        *details,
        type=type(e).__name__,
        non_retryable=not e.effective_retryable,
    )
