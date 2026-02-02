"""Lazy evaluation utilities for integration testing.

This module provides lazy evaluation wrappers that defer computation until
the value is actually needed. This is useful for:

1. Credential loading - Don't load credentials at import time, only when tests run
2. Environment-specific values - Allow tests to be defined in one environment, run in another
3. Caching - Expensive computations are only performed once

Example:
    >>> from application_sdk.test_utils.integration import lazy
    >>> 
    >>> # Value is not computed until evaluate() is called
    >>> creds = lazy(lambda: load_credentials_from_env("MY_APP"))
    >>> 
    >>> # Later, when test runs:
    >>> actual_creds = creds.evaluate()  # Now it loads
    >>> actual_creds_again = creds.evaluate()  # Returns cached value
"""

from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class Lazy(Generic[T]):
    """Wrapper for lazy evaluation with caching.

    The wrapped function is not called until evaluate() is invoked.
    Once evaluated, the result is cached and returned on subsequent calls.

    Attributes:
        _fn: The function to evaluate lazily.
        _cached: The cached result after evaluation.
        _evaluated: Whether the function has been evaluated.

    Example:
        >>> expensive_value = Lazy(lambda: compute_expensive_thing())
        >>> # Nothing computed yet
        >>> result = expensive_value.evaluate()  # Now it computes
        >>> result2 = expensive_value.evaluate()  # Returns cached value
    """

    def __init__(self, fn: Callable[[], T]):
        """Initialize the lazy wrapper.

        Args:
            fn: A callable that takes no arguments and returns the value.
        """
        if not callable(fn):
            raise TypeError("Lazy wrapper requires a callable")
        self._fn = fn
        self._cached: T = None  # type: ignore
        self._evaluated: bool = False

    def evaluate(self) -> T:
        """Evaluate the wrapped function and return the result.

        The function is only called on the first invocation.
        Subsequent calls return the cached result.

        Returns:
            T: The result of calling the wrapped function.

        Raises:
            Any exception raised by the wrapped function.
        """
        if not self._evaluated:
            self._cached = self._fn()
            self._evaluated = True
        return self._cached

    def is_evaluated(self) -> bool:
        """Check if the value has been evaluated.

        Returns:
            bool: True if evaluate() has been called, False otherwise.
        """
        return self._evaluated

    def reset(self) -> None:
        """Reset the lazy wrapper to re-evaluate on next access.

        This clears the cached value and allows the function to be
        called again on the next evaluate() call.
        """
        self._cached = None  # type: ignore
        self._evaluated = False

    def __repr__(self) -> str:
        """String representation of the lazy wrapper."""
        status = "evaluated" if self._evaluated else "unevaluated"
        return f"Lazy({status})"


def lazy(fn: Callable[[], T]) -> Lazy[T]:
    """Create a lazy evaluation wrapper.

    This is the primary interface for creating lazy values.

    Args:
        fn: A callable that takes no arguments and returns the value.

    Returns:
        Lazy[T]: A lazy wrapper around the function.

    Example:
        >>> from application_sdk.test_utils.integration import lazy
        >>> 
        >>> def load_creds():
        ...     return {"username": "test", "password": "secret"}
        >>> 
        >>> creds = lazy(load_creds)
        >>> # Or with lambda:
        >>> creds = lazy(lambda: {"username": "test", "password": "secret"})
    """
    return Lazy(fn)


def is_lazy(value: Any) -> bool:
    """Check if a value is a lazy wrapper.

    Args:
        value: Any value to check.

    Returns:
        bool: True if the value is a Lazy wrapper, False otherwise.

    Example:
        >>> from application_sdk.test_utils.integration import lazy, is_lazy
        >>> 
        >>> value = lazy(lambda: 42)
        >>> is_lazy(value)  # True
        >>> is_lazy(42)     # False
    """
    return isinstance(value, Lazy)


def evaluate_if_lazy(value: T) -> T:
    """Evaluate a value if it's lazy, otherwise return as-is.

    This is a convenience function for handling values that may or may not
    be lazy-wrapped.

    Args:
        value: A value that may be a Lazy wrapper or a regular value.

    Returns:
        T: The evaluated value (if lazy) or the original value.

    Example:
        >>> from application_sdk.test_utils.integration import lazy, evaluate_if_lazy
        >>> 
        >>> lazy_value = lazy(lambda: 42)
        >>> regular_value = 42
        >>> 
        >>> evaluate_if_lazy(lazy_value)   # Returns 42
        >>> evaluate_if_lazy(regular_value) # Returns 42
    """
    if is_lazy(value):
        return value.evaluate()
    return value
