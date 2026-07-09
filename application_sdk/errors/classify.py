"""Reusable mechanics for turning a raw source error into a typed :class:`AppError`.

Two building blocks that apps (and the SDK itself) kept re-implementing by hand:

* :func:`causal_chain` — the bounded, cycle-safe walk over ``__cause__`` /
  ``__context__`` that every "find the classified error somewhere in the chain"
  loop needs.
* :func:`errno_classifier` — a factory that turns an app-owned ``{errno: leaf}``
  mapping into a classifier callable. The mapping is *app knowledge* (driver
  codes, their messages, their remediation); this factory owns only the
  mechanics (chain walk + DBAPI errno extraction), so apps stop hand-rolling
  cause-walking and errno digging.

Classification is always **explicit**: a caller opts in by building a mapping
and consulting the classifier at the boundary where the error occurs. There is
no global/implicit hook — an unclassified error returns ``None`` so the caller
falls back to :meth:`PreflightCheck.from_error`'s sanitized generic rather than
the SDK guessing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping

from application_sdk.errors.base import AppError

# Max nodes walked before the chain is abandoned. Must stay equal to
# ``activities._MAX_CHAIN_DEPTH`` (the sever cap) and
# ``interceptors.log._MAX_CHAIN_WALK``: an AppError sitting past this depth is
# already severed off the wire, so walking further would only find nodes that
# never reach a consumer.
_CAUSAL_CHAIN_LIMIT = 50


def causal_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield ``exc`` then its ``__cause__`` / ``__context__`` chain.

    Follows ``__cause__`` first, falling back to ``__context__`` — the same
    precedence Python uses when printing a traceback. Bounded to
    ``_CAUSAL_CHAIN_LIMIT`` nodes and cycle-safe via an identity set, so a
    self-referential or pathologically deep chain terminates instead of looping.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while (
        current is not None
        and id(current) not in seen
        and len(seen) < _CAUSAL_CHAIN_LIMIT
    ):
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _errno_candidates(exc: BaseException) -> Iterator[int]:
    """Yield the DBAPI-style errno(s) an exception exposes, if any.

    Drivers surface the numeric code either as the first positional arg
    (``exc.args[0]``, the common DBAPI shape) or as an ``.errno`` attribute.
    ``bool`` is excluded — it is an ``int`` subclass but never a real errno.
    """
    args = getattr(exc, "args", None)
    if args and isinstance(args[0], int) and not isinstance(args[0], bool):
        yield args[0]
    errno_attr = getattr(exc, "errno", None)
    if isinstance(errno_attr, int) and not isinstance(errno_attr, bool):
        yield errno_attr


def errno_classifier(
    mapping: Mapping[int, type[AppError]],
) -> Callable[[BaseException], AppError | None]:
    """Build a classifier that maps a driver errno to a typed :class:`AppError`.

    The returned callable walks :func:`causal_chain` of the exception it is
    given, extracts a DBAPI-style errno from each node (positional ``args[0]``
    or an ``.errno`` attribute), and returns ``mapping[errno]()`` on the first
    hit. Returns ``None`` when nothing in the chain matches — the caller must
    fall back to the sanitized generic (e.g. :meth:`PreflightCheck.from_error`),
    never guess.

    ``mapping`` is app knowledge: the driver-specific error codes plus the leaf
    (and thus the curated message / category / suggested action) each maps to.
    This factory owns only the mechanics.
    """

    def classify(exc: BaseException) -> AppError | None:
        if not mapping:
            return None
        for node in causal_chain(exc):
            for errno in _errno_candidates(node):
                leaf = mapping.get(errno)
                if leaf is not None:
                    # Mapping values are app leaves that carry their curated
                    # default message, so they construct with no args — the base
                    # AppError signature (message required) can't express that.
                    return leaf()  # type: ignore[call-arg]
        return None

    return classify
