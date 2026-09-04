"""BLDX-1625 proposal: reraise_unless_tolerated must not let an unlisted
exception — especially an already-typed AppError — get swallowed."""

from __future__ import annotations

import pytest

from application_sdk.common.exception_guards import reraise_unless_tolerated
from application_sdk.errors.leaves import AuthError, RateLimitedError


class CatalogNotFoundError(Exception):
    pass


def test_tolerated_type_is_a_no_op():
    reraise_unless_tolerated(
        CatalogNotFoundError("gone"), tolerated=(CatalogNotFoundError,)
    )


def test_untolerated_type_escapes():
    with pytest.raises(ValueError, match="boom"):
        reraise_unless_tolerated(ValueError("boom"), tolerated=(CatalogNotFoundError,))


def test_a_typed_app_error_escapes_even_with_a_broad_tolerated_set():
    """The whole point: a caller tolerating plain Exception subclasses must
    not accidentally swallow a classification a lower layer already made."""
    with pytest.raises(RateLimitedError):
        reraise_unless_tolerated(
            RateLimitedError(message="throttled"), tolerated=(CatalogNotFoundError,)
        )


def test_subclass_of_a_tolerated_type_is_also_a_no_op():
    class SpecificNotFound(CatalogNotFoundError):
        pass

    reraise_unless_tolerated(
        SpecificNotFound("gone"), tolerated=(CatalogNotFoundError,)
    )


def test_empty_tolerated_set_always_reraises():
    with pytest.raises(AuthError):
        reraise_unless_tolerated(AuthError(message="bad token"), tolerated=())
