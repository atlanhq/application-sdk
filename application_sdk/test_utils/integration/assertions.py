"""Assertion DSL for integration testing.

This module provides higher-order functions that return predicates for use
in scenario assertions. Each function returns a callable that takes an
actual value and returns True/False.

The design follows functional programming principles:
- Higher-order functions: Functions that return functions
- Composability: Assertions can be combined using all_of/any_of
- Declarative: Describe what to check, not how

Example:
    >>> from application_sdk.test_utils.integration import Scenario, equals, exists, one_of
    >>> 
    >>> Scenario(
    ...     name="auth_test",
    ...     api="auth",
    ...     args={"credentials": {...}},
    ...     assert_that={
    ...         "success": equals(True),
    ...         "data.user_id": exists(),
    ...         "data.role": one_of(["admin", "user"]),
    ...     }
    ... )
"""

import re
from typing import Any, Callable, List, Pattern, Union

# Type alias for predicate functions
Predicate = Callable[[Any], bool]


# =============================================================================
# Basic Assertions
# =============================================================================


def equals(expected: Any) -> Predicate:
    """Assert that the actual value equals the expected value.

    Args:
        expected: The expected value.

    Returns:
        Predicate: A function that returns True if actual == expected.

    Example:
        >>> check = equals(True)
        >>> check(True)   # True
        >>> check(False)  # False
    """

    def predicate(actual: Any) -> bool:
        return actual == expected

    predicate.__doc__ = f"equals({expected!r})"
    return predicate


def not_equals(unexpected: Any) -> Predicate:
    """Assert that the actual value does not equal the unexpected value.

    Args:
        unexpected: The value that should not match.

    Returns:
        Predicate: A function that returns True if actual != unexpected.

    Example:
        >>> check = not_equals(None)
        >>> check("value")  # True
        >>> check(None)     # False
    """

    def predicate(actual: Any) -> bool:
        return actual != unexpected

    predicate.__doc__ = f"not_equals({unexpected!r})"
    return predicate


def exists() -> Predicate:
    """Assert that the actual value is not None.

    Returns:
        Predicate: A function that returns True if actual is not None.

    Example:
        >>> check = exists()
        >>> check("value")  # True
        >>> check(None)     # False
    """

    def predicate(actual: Any) -> bool:
        return actual is not None

    predicate.__doc__ = "exists()"
    return predicate


def is_none() -> Predicate:
    """Assert that the actual value is None.

    Returns:
        Predicate: A function that returns True if actual is None.

    Example:
        >>> check = is_none()
        >>> check(None)     # True
        >>> check("value")  # False
    """

    def predicate(actual: Any) -> bool:
        return actual is None

    predicate.__doc__ = "is_none()"
    return predicate


def is_true() -> Predicate:
    """Assert that the actual value is truthy.

    Returns:
        Predicate: A function that returns True if bool(actual) is True.

    Example:
        >>> check = is_true()
        >>> check(True)   # True
        >>> check(1)      # True
        >>> check("")     # False
    """

    def predicate(actual: Any) -> bool:
        return bool(actual)

    predicate.__doc__ = "is_true()"
    return predicate


def is_false() -> Predicate:
    """Assert that the actual value is falsy.

    Returns:
        Predicate: A function that returns True if bool(actual) is False.

    Example:
        >>> check = is_false()
        >>> check(False)  # True
        >>> check(0)      # True
        >>> check("x")    # False
    """

    def predicate(actual: Any) -> bool:
        return not bool(actual)

    predicate.__doc__ = "is_false()"
    return predicate


# =============================================================================
# Collection Assertions
# =============================================================================


def one_of(options: List[Any]) -> Predicate:
    """Assert that the actual value is one of the given options.

    Args:
        options: List of valid values.

    Returns:
        Predicate: A function that returns True if actual is in options.

    Example:
        >>> check = one_of(["admin", "user", "guest"])
        >>> check("admin")    # True
        >>> check("unknown")  # False
    """

    def predicate(actual: Any) -> bool:
        return actual in options

    predicate.__doc__ = f"one_of({options!r})"
    return predicate


def not_one_of(excluded: List[Any]) -> Predicate:
    """Assert that the actual value is not one of the given values.

    Args:
        excluded: List of values that should not match.

    Returns:
        Predicate: A function that returns True if actual is not in excluded.

    Example:
        >>> check = not_one_of(["error", "failed"])
        >>> check("success")  # True
        >>> check("error")    # False
    """

    def predicate(actual: Any) -> bool:
        return actual not in excluded

    predicate.__doc__ = f"not_one_of({excluded!r})"
    return predicate


def contains(item: Any) -> Predicate:
    """Assert that the actual value contains the given item.

    Works for strings (substring check) and collections (membership check).

    Args:
        item: The item to search for.

    Returns:
        Predicate: A function that returns True if item is in actual.

    Example:
        >>> check = contains("error")
        >>> check("An error occurred")  # True
        >>> check("Success")            # False
        >>> 
        >>> check = contains(42)
        >>> check([1, 42, 3])  # True
    """

    def predicate(actual: Any) -> bool:
        try:
            return item in actual
        except TypeError:
            return False

    predicate.__doc__ = f"contains({item!r})"
    return predicate


def not_contains(item: Any) -> Predicate:
    """Assert that the actual value does not contain the given item.

    Args:
        item: The item that should not be present.

    Returns:
        Predicate: A function that returns True if item is not in actual.

    Example:
        >>> check = not_contains("password")
        >>> check("user logged in")  # True
        >>> check("password: 123")   # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return item not in actual
        except TypeError:
            return True

    predicate.__doc__ = f"not_contains({item!r})"
    return predicate


def has_length(expected_length: int) -> Predicate:
    """Assert that the actual value has the expected length.

    Args:
        expected_length: The expected length.

    Returns:
        Predicate: A function that returns True if len(actual) == expected_length.

    Example:
        >>> check = has_length(3)
        >>> check([1, 2, 3])  # True
        >>> check("abc")      # True
        >>> check([1, 2])     # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return len(actual) == expected_length
        except TypeError:
            return False

    predicate.__doc__ = f"has_length({expected_length})"
    return predicate


def is_empty() -> Predicate:
    """Assert that the actual value is empty.

    Returns:
        Predicate: A function that returns True if actual is empty.

    Example:
        >>> check = is_empty()
        >>> check([])    # True
        >>> check("")    # True
        >>> check([1])   # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return len(actual) == 0
        except TypeError:
            return False

    predicate.__doc__ = "is_empty()"
    return predicate


def is_not_empty() -> Predicate:
    """Assert that the actual value is not empty.

    Returns:
        Predicate: A function that returns True if actual is not empty.

    Example:
        >>> check = is_not_empty()
        >>> check([1])   # True
        >>> check("x")   # True
        >>> check([])    # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return len(actual) > 0
        except TypeError:
            return False

    predicate.__doc__ = "is_not_empty()"
    return predicate


# =============================================================================
# Numeric Assertions
# =============================================================================


def greater_than(value: Union[int, float]) -> Predicate:
    """Assert that the actual value is greater than the given value.

    Args:
        value: The value to compare against.

    Returns:
        Predicate: A function that returns True if actual > value.

    Example:
        >>> check = greater_than(0)
        >>> check(1)   # True
        >>> check(0)   # False
        >>> check(-1)  # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return actual > value
        except TypeError:
            return False

    predicate.__doc__ = f"greater_than({value})"
    return predicate


def greater_than_or_equal(value: Union[int, float]) -> Predicate:
    """Assert that the actual value is greater than or equal to the given value.

    Args:
        value: The value to compare against.

    Returns:
        Predicate: A function that returns True if actual >= value.

    Example:
        >>> check = greater_than_or_equal(0)
        >>> check(1)   # True
        >>> check(0)   # True
        >>> check(-1)  # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return actual >= value
        except TypeError:
            return False

    predicate.__doc__ = f"greater_than_or_equal({value})"
    return predicate


def less_than(value: Union[int, float]) -> Predicate:
    """Assert that the actual value is less than the given value.

    Args:
        value: The value to compare against.

    Returns:
        Predicate: A function that returns True if actual < value.

    Example:
        >>> check = less_than(10)
        >>> check(5)   # True
        >>> check(10)  # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return actual < value
        except TypeError:
            return False

    predicate.__doc__ = f"less_than({value})"
    return predicate


def less_than_or_equal(value: Union[int, float]) -> Predicate:
    """Assert that the actual value is less than or equal to the given value.

    Args:
        value: The value to compare against.

    Returns:
        Predicate: A function that returns True if actual <= value.

    Example:
        >>> check = less_than_or_equal(10)
        >>> check(5)   # True
        >>> check(10)  # True
        >>> check(11)  # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return actual <= value
        except TypeError:
            return False

    predicate.__doc__ = f"less_than_or_equal({value})"
    return predicate


def between(min_value: Union[int, float], max_value: Union[int, float]) -> Predicate:
    """Assert that the actual value is between min and max (inclusive).

    Args:
        min_value: The minimum value (inclusive).
        max_value: The maximum value (inclusive).

    Returns:
        Predicate: A function that returns True if min_value <= actual <= max_value.

    Example:
        >>> check = between(1, 10)
        >>> check(5)   # True
        >>> check(1)   # True
        >>> check(0)   # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return min_value <= actual <= max_value
        except TypeError:
            return False

    predicate.__doc__ = f"between({min_value}, {max_value})"
    return predicate


# =============================================================================
# String Assertions
# =============================================================================


def matches(pattern: Union[str, Pattern]) -> Predicate:
    """Assert that the actual value matches the given regex pattern.

    Args:
        pattern: A regex pattern string or compiled pattern.

    Returns:
        Predicate: A function that returns True if actual matches the pattern.

    Example:
        >>> check = matches(r"^[a-z]+$")
        >>> check("hello")  # True
        >>> check("Hello")  # False
        >>> check("123")    # False
    """
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern

    def predicate(actual: Any) -> bool:
        if actual is None:
            return False
        return compiled.match(str(actual)) is not None

    predicate.__doc__ = f"matches({pattern!r})"
    return predicate


def starts_with(prefix: str) -> Predicate:
    """Assert that the actual value starts with the given prefix.

    Args:
        prefix: The expected prefix.

    Returns:
        Predicate: A function that returns True if actual starts with prefix.

    Example:
        >>> check = starts_with("http")
        >>> check("https://example.com")  # True
        >>> check("ftp://example.com")    # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return str(actual).startswith(prefix)
        except (TypeError, AttributeError):
            return False

    predicate.__doc__ = f"starts_with({prefix!r})"
    return predicate


def ends_with(suffix: str) -> Predicate:
    """Assert that the actual value ends with the given suffix.

    Args:
        suffix: The expected suffix.

    Returns:
        Predicate: A function that returns True if actual ends with suffix.

    Example:
        >>> check = ends_with(".json")
        >>> check("data.json")  # True
        >>> check("data.xml")   # False
    """

    def predicate(actual: Any) -> bool:
        try:
            return str(actual).endswith(suffix)
        except (TypeError, AttributeError):
            return False

    predicate.__doc__ = f"ends_with({suffix!r})"
    return predicate


# =============================================================================
# Type Assertions
# =============================================================================


def is_type(expected_type: type) -> Predicate:
    """Assert that the actual value is an instance of the given type.

    Args:
        expected_type: The expected type.

    Returns:
        Predicate: A function that returns True if isinstance(actual, expected_type).

    Example:
        >>> check = is_type(str)
        >>> check("hello")  # True
        >>> check(123)      # False
    """

    def predicate(actual: Any) -> bool:
        return isinstance(actual, expected_type)

    predicate.__doc__ = f"is_type({expected_type.__name__})"
    return predicate


def is_dict() -> Predicate:
    """Assert that the actual value is a dictionary.

    Returns:
        Predicate: A function that returns True if actual is a dict.

    Example:
        >>> check = is_dict()
        >>> check({"key": "value"})  # True
        >>> check([1, 2, 3])         # False
    """
    return is_type(dict)


def is_list() -> Predicate:
    """Assert that the actual value is a list.

    Returns:
        Predicate: A function that returns True if actual is a list.

    Example:
        >>> check = is_list()
        >>> check([1, 2, 3])  # True
        >>> check("abc")      # False
    """
    return is_type(list)


def is_string() -> Predicate:
    """Assert that the actual value is a string.

    Returns:
        Predicate: A function that returns True if actual is a str.

    Example:
        >>> check = is_string()
        >>> check("hello")  # True
        >>> check(123)      # False
    """
    return is_type(str)


# =============================================================================
# Combinators (Compose Assertions)
# =============================================================================


def all_of(*predicates: Predicate) -> Predicate:
    """Assert that all predicates pass.

    Args:
        *predicates: Variable number of predicates to combine.

    Returns:
        Predicate: A function that returns True if all predicates pass.

    Example:
        >>> check = all_of(exists(), is_string(), starts_with("http"))
        >>> check("https://example.com")  # True
        >>> check(None)                   # False
    """

    def predicate(actual: Any) -> bool:
        return all(p(actual) for p in predicates)

    predicate.__doc__ = f"all_of({len(predicates)} predicates)"
    return predicate


def any_of(*predicates: Predicate) -> Predicate:
    """Assert that at least one predicate passes.

    Args:
        *predicates: Variable number of predicates to combine.

    Returns:
        Predicate: A function that returns True if any predicate passes.

    Example:
        >>> check = any_of(equals("admin"), equals("superuser"))
        >>> check("admin")      # True
        >>> check("superuser")  # True
        >>> check("guest")      # False
    """

    def predicate(actual: Any) -> bool:
        return any(p(actual) for p in predicates)

    predicate.__doc__ = f"any_of({len(predicates)} predicates)"
    return predicate


def none_of(*predicates: Predicate) -> Predicate:
    """Assert that none of the predicates pass.

    Args:
        *predicates: Variable number of predicates to combine.

    Returns:
        Predicate: A function that returns True if no predicate passes.

    Example:
        >>> check = none_of(contains("error"), contains("fail"))
        >>> check("success")     # True
        >>> check("error found") # False
    """

    def predicate(actual: Any) -> bool:
        return not any(p(actual) for p in predicates)

    predicate.__doc__ = f"none_of({len(predicates)} predicates)"
    return predicate


# =============================================================================
# Custom Assertion
# =============================================================================


def custom(fn: Callable[[Any], bool], description: str = "custom") -> Predicate:
    """Create a custom assertion from a user-provided function.

    Args:
        fn: A function that takes the actual value and returns True/False.
        description: Optional description for error messages.

    Returns:
        Predicate: The function wrapped as a predicate.

    Example:
        >>> check = custom(lambda x: x % 2 == 0, "is_even")
        >>> check(4)  # True
        >>> check(3)  # False
    """
    fn.__doc__ = description
    return fn
