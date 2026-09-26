"""The leaf set and the HTTP mapping must both cover every FailureCategory.

Both invariants were broken and the breakage was invisible: only four of the
fifteen categories had a leaf, so connectors redeclared `category` on AppError
subclasses instead (taxonomy drift, P002); and SOURCE_UNAVAILABLE had no status
mapping at all, so it fell through to the 500 default and a customer source
being down read as this service failing.
"""

from __future__ import annotations

import inspect

import pytest
from server_sdk.errors import leaves as leaves_mod
from server_sdk.errors.base import AppError
from server_sdk.errors.categories import FailureCategory
from server_sdk.handler.contracts import _AUTH_STATUS_HTTP_CODES, AuthStatus
from server_sdk.server import _CATEGORY_TO_HTTP, _app_error_to_http_status


def _leaves() -> list[type[AppError]]:
    return [
        obj
        for _, obj in inspect.getmembers(leaves_mod, inspect.isclass)
        if issubclass(obj, AppError) and obj is not AppError
    ]


def test_every_category_has_exactly_one_leaf() -> None:
    by_category: dict[FailureCategory, list[str]] = {}
    for leaf in _leaves():
        by_category.setdefault(leaf.category, []).append(leaf.__name__)

    missing = sorted(c.name for c in FailureCategory if c not in by_category)
    assert not missing, f"FailureCategory members with no leaf to inherit: {missing}"

    duplicated = {c.name: names for c, names in by_category.items() if len(names) > 1}
    assert not duplicated, f"more than one leaf per category: {duplicated}"


def test_every_category_maps_to_an_http_status() -> None:
    missing = sorted(c.name for c in FailureCategory if c not in _CATEGORY_TO_HTTP)
    assert not missing, f"categories with no HTTP mapping (silently 500): {missing}"


@pytest.mark.parametrize("leaf", _leaves(), ids=lambda c: c.__name__)
def test_leaf_declares_its_own_code_and_resolves_to_a_status(leaf) -> None:
    # Every leaf names itself after its category -- InternalError's code equals
    # AppError's default for that reason, not by omission.
    assert leaf.code == leaf.category.value, (
        f"{leaf.__name__}: code {leaf.code!r} should be its category "
        f"{leaf.category.value!r}"
    )
    status = _app_error_to_http_status(leaf("boom"))
    assert 400 <= status <= 599


def test_source_unavailable_is_503_not_500() -> None:
    """The customer's source being down is not this service failing."""
    assert _app_error_to_http_status(leaves_mod.SourceUnavailableError("down")) == 503


# ---------------------------------------------------------------------------
# Value pinning
# ---------------------------------------------------------------------------
# The bug recorded above was a *wrong* status, not a missing key, and the tests
# written for it only check presence -- AUTH could become 500 with the suite
# still green. Heracles branches on these codes, so each pair is contract.

_EXPECTED_STATUS: dict[FailureCategory, int] = {
    FailureCategory.AUTH: 401,
    FailureCategory.PERMISSION: 403,
    FailureCategory.NOT_FOUND: 404,
    FailureCategory.ALREADY_EXISTS: 409,
    FailureCategory.INVALID_INPUT: 400,
    FailureCategory.PRECONDITION: 412,
    FailureCategory.RATE_LIMITED: 429,
    FailureCategory.TIMEOUT: 504,
    FailureCategory.DEPENDENCY_UNAVAILABLE: 503,
    FailureCategory.SOURCE_UNAVAILABLE: 503,
    FailureCategory.RESOURCE_EXHAUSTED: 503,
    FailureCategory.DATA_INTEGRITY: 500,
    FailureCategory.INTERNAL: 500,
    FailureCategory.UNIMPLEMENTED: 501,
    FailureCategory.CANCELLED: 499,
}


def test_the_pinned_status_table_covers_every_category() -> None:
    """A new category must be given a status here, not just anywhere."""
    assert set(_EXPECTED_STATUS) == set(FailureCategory)


@pytest.mark.parametrize(
    ("category", "status"),
    sorted(_EXPECTED_STATUS.items(), key=lambda kv: kv[0].name),  # pyright: ignore[reportIndexIssue,reportUnknownLambdaType]
    ids=lambda v: v.name if isinstance(v, FailureCategory) else str(v),
)
def test_each_category_pins_its_http_status(
    category: FailureCategory, status: int
) -> None:
    assert _CATEGORY_TO_HTTP[category] == status


@pytest.mark.parametrize("leaf", _leaves(), ids=lambda c: c.__name__)
def test_leaf_resolves_to_its_categorys_pinned_status(leaf) -> None:
    assert _app_error_to_http_status(leaf("boom")) == _EXPECTED_STATUS[leaf.category]


# ---------------------------------------------------------------------------
# AuthStatus -- the same invariant, on the sibling enum
# ---------------------------------------------------------------------------

_EXPECTED_AUTH_STATUS: dict[AuthStatus, int] = {
    AuthStatus.SUCCESS: 200,
    AuthStatus.FAILED: 401,
    AuthStatus.EXPIRED: 401,
    AuthStatus.INVALID_CREDENTIALS: 401,
}


def test_auth_status_map_covers_every_member() -> None:
    """Its comment leans on a runtime KeyError -- which lands on a tenant's
    credential test rather than in CI."""
    assert set(_AUTH_STATUS_HTTP_CODES) == set(AuthStatus)
    assert set(_EXPECTED_AUTH_STATUS) == set(AuthStatus)


@pytest.mark.parametrize(
    ("status", "code"),
    sorted(_EXPECTED_AUTH_STATUS.items(), key=lambda kv: kv[0].name),  # pyright: ignore[reportIndexIssue,reportUnknownLambdaType]
    ids=lambda v: v.name if isinstance(v, AuthStatus) else str(v),
)
def test_each_auth_status_pins_its_code(status: AuthStatus, code: int) -> None:
    assert status.http_status == code
    assert status.is_success is (code < 400)
