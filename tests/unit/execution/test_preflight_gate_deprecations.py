"""The deprecated compatibility surface #3685 removed without a cycle.

3.36.0 shipped seven names gone from ``preflight_gate``. Every consumer that
imported one broke on ``ImportError`` at its next lock refresh — 15 repos, all in
test suites — with no ``DeprecationWarning`` and no migration window. These tests
pin the restored surface on two axes:

* **value parity** — each alias resolves to exactly what the pre-3.36.0 name held,
  so a consumer on the shim behaves identically to one on 3.35.0;
* **the nudge** — each access or call emits a ``DeprecationWarning`` naming the
  replacement and the removal version, because a shim that migrates nobody just
  moves the same breakage to 3.40.0.

The literals here are transcribed from the module at ``v3.35.0``, not from the
current source, so they keep their meaning if the replacements are refactored
again: a test asserting ``alias == replacement`` would pass even if both drifted
off the value consumers actually depend on.
"""

from __future__ import annotations

import warnings

import pytest

from application_sdk.errors.base import AppError
from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import (
    AuthError,
    CancelledError,
    DependencyUnavailableError,
    RateLimitedError,
    ResourceExhaustedError,
    SourceUnavailableError,
)
from application_sdk.execution._temporal import preflight_gate
from application_sdk.execution._temporal.preflight_gate import (
    DEPRECATED_FAIL_OPEN_REMOVED_IN,
    GATE_ATTEMPTS_DEFAULT,
    GATE_ATTEMPTS_MAX,
    GATE_ATTEMPTS_MIN,
    GATE_TIMEOUT_DEFAULT_SECONDS,
    GATE_TIMEOUT_MAX_SECONDS,
    GATE_TIMEOUT_MIN_SECONDS,
    PreflightClassification,
    PreflightGateMode,
)

# Transcribed from application_sdk/execution/_temporal/preflight_gate.py at v3.35.0.
_V3_35_CLASSIFICATION_VERDICT = "verdict"
_V3_35_CLASSIFICATION_GATE_BROKEN = "gate_broken"
_V3_35_CLASSIFICATION_SOURCE_UNVERIFIABLE = "source_unverifiable"
_V3_35_UNVERIFIABLE_CHECK_NAME = "preflightVerdict"
_V3_35_GATE_BROKEN_CATEGORIES = frozenset(
    {
        FailureCategory.DEPENDENCY_UNAVAILABLE,
        FailureCategory.RATE_LIMITED,
        FailureCategory.RESOURCE_EXHAUSTED,
        FailureCategory.CANCELLED,
    }
)

_DEPRECATED_CONSTANT_NAMES = (
    "_GATE_BROKEN_CATEGORIES",
    "CLASSIFICATION_VERDICT",
    "CLASSIFICATION_GATE_BROKEN",
    "CLASSIFICATION_SOURCE_UNVERIFIABLE",
    "GATE_RETRY",
    "UNVERIFIABLE_CHECK_NAME",
)


class TestDeprecatedConstantValues:
    """Each alias still holds exactly what 3.35.0 held."""

    def test_classification_constants_keep_their_wire_values(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert (
                preflight_gate.CLASSIFICATION_VERDICT == _V3_35_CLASSIFICATION_VERDICT
            )
            assert (
                preflight_gate.CLASSIFICATION_GATE_BROKEN
                == _V3_35_CLASSIFICATION_GATE_BROKEN
            )
            assert (
                preflight_gate.CLASSIFICATION_SOURCE_UNVERIFIABLE
                == _V3_35_CLASSIFICATION_SOURCE_UNVERIFIABLE
            )

    def test_classification_aliases_are_the_str_value_not_the_enum(self) -> None:
        """3.35.0 held plain strings; a consumer may serialise or compare them.

        Handing back the ``PreflightClassification`` member instead would pass an
        ``==`` against the string on a ``str``-mixin enum but change ``type()``,
        ``json.dumps`` and dict-key identity for everyone on the shim.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            value = preflight_gate.CLASSIFICATION_VERDICT
        assert type(value) is str
        assert value == PreflightClassification.VERDICT.value

    def test_gate_broken_categories_is_the_same_four_members(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert (
                preflight_gate._GATE_BROKEN_CATEGORIES == _V3_35_GATE_BROKEN_CATEGORIES
            )

    def test_unverifiable_check_name_survives_its_move_to_contracts(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert (
                preflight_gate.UNVERIFIABLE_CHECK_NAME == _V3_35_UNVERIFIABLE_CHECK_NAME
            )

    def test_gate_retry_matches_the_policy_3_35_shipped(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            policy = preflight_gate.GATE_RETRY
        assert policy.maximum_attempts == 2
        assert policy.backoff_coefficient == 2


class TestDeprecationNudge:
    """A shim that warns nobody just moves the same breakage to 3.40.0."""

    @pytest.mark.parametrize("name", _DEPRECATED_CONSTANT_NAMES)
    def test_every_constant_access_warns(self, name: str) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            getattr(preflight_gate, name)
        assert len(caught) == 1
        assert issubclass(caught[0].category, DeprecationWarning)

    @pytest.mark.parametrize("name", _DEPRECATED_CONSTANT_NAMES)
    def test_every_constant_notice_names_target_and_removal(self, name: str) -> None:
        """B002's two requirements, asserted at the notice a caller actually sees."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            getattr(preflight_gate, name)
        message = str(caught[0].message)
        assert "use " in message
        assert f"removed in v{DEPRECATED_FAIL_OPEN_REMOVED_IN}" in message

    def test_deprecated_functions_warn_when_called(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            preflight_gate.resolve_gate_budget_seconds(30)
            preflight_gate.resolve_gate_attempts(2)
            preflight_gate._is_gate_broken(ValueError("x"))
        assert len(caught) == 3
        assert all(issubclass(c.category, DeprecationWarning) for c in caught)

    def test_unknown_attribute_still_raises(self) -> None:
        """``__getattr__`` must not turn every typo into a silent ``None``."""
        with pytest.raises(AttributeError, match="no attribute"):
            preflight_gate.definitely_not_a_gate_symbol


class TestDeprecatedEnforceKeyword:
    """The ``enforce`` keyword #3685 replaced with ``mode``.

    Not a removed *name*, so the blast-radius count built from names missed it —
    `build_preflight_gate_activity` still exists, it just rejects the keyword
    every caller passes. One live caller is known (a connector's gate test
    passing ``enforce=False``); on `log_gate_posture` ``enforce`` was
    keyword-*required*, so every caller of it passed one.
    """

    def test_builder_accepts_the_live_caller_shape(self) -> None:
        """The exact call shape found in a connector's gate test."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            activity = preflight_gate.build_preflight_gate_activity(
                object(),  # type: ignore[arg-type]
                "teradata",
                enforce=False,
                budget_seconds=30,
                attempts=2,
            )
        assert callable(activity)
        assert len(caught) == 1
        assert issubclass(caught[0].category, DeprecationWarning)

    @pytest.mark.parametrize(
        ("enforce", "enforces"),
        [(True, True), (False, False)],
    )
    def test_enforce_maps_onto_the_posture_it_meant(
        self, enforce: bool, enforces: bool
    ) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            resolved = preflight_gate._mode_from_deprecated_enforce(
                callable_name="t", mode=None, enforce=enforce, default=None
            )
        assert resolved.enforces is enforces

    def test_log_gate_posture_accepts_the_deprecated_keyword(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            preflight_gate.log_gate_posture("app", enforce=True, budget_seconds=30)
        assert len(caught) == 1
        assert issubclass(caught[0].category, DeprecationWarning)

    def test_notice_names_target_and_removal(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            preflight_gate.log_gate_posture("app", enforce=False, budget_seconds=30)
        message = str(caught[0].message)
        assert "use mode=" in message
        assert f"removed in v{DEPRECATED_FAIL_OPEN_REMOVED_IN}" in message

    def test_both_spellings_is_a_type_error(self) -> None:
        """Silently picking a winner would hide a half-finished migration."""
        with pytest.raises(TypeError, match="both 'mode' and the deprecated"):
            preflight_gate.log_gate_posture(
                "app",
                mode=PreflightGateMode.HARD,
                enforce=True,
                budget_seconds=30,
            )

    def test_mode_stays_required_where_it_was(self) -> None:
        """``mode`` only carries a default so an ``enforce`` caller can omit it."""
        with pytest.raises(TypeError, match="missing required keyword"):
            preflight_gate.log_gate_posture("app", budget_seconds=30)

    def test_builder_still_defaults_to_soft(self) -> None:
        """Omitting both kept v3.35.0's ``enforce=False`` default."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            resolved = preflight_gate._mode_from_deprecated_enforce(
                callable_name="build_preflight_gate_activity",
                mode=None,
                enforce=None,
                default=PreflightGateMode.SOFT,
            )
        assert resolved is PreflightGateMode.SOFT
        assert resolved.enforces is False
        assert caught == []

    def test_mode_alone_does_not_warn(self) -> None:
        """Callers already migrated pay nothing."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            preflight_gate.log_gate_posture(
                "app", mode=PreflightGateMode.HARD, budget_seconds=30
            )
        assert caught == []


class TestDeprecatedResolvers:
    """The two resolvers still clamp and return a bare int, as 3.35.0 did."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (30, 30),
            # Out of range clamps to the bound; only an unusable declaration
            # falls back to the default. 3.35.0 drew the line the same way.
            (GATE_TIMEOUT_MAX_SECONDS + 1, GATE_TIMEOUT_MAX_SECONDS),
            (0, GATE_TIMEOUT_MIN_SECONDS),
            ("not-a-number", GATE_TIMEOUT_DEFAULT_SECONDS),
            (None, GATE_TIMEOUT_DEFAULT_SECONDS),
        ],
    )
    def test_budget_clamps_and_returns_an_int(self, raw: object, expected: int) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            resolved = preflight_gate.resolve_gate_budget_seconds(raw)
        assert resolved == expected
        assert type(resolved) is int

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (1, 1),
            (GATE_ATTEMPTS_MAX + 1, GATE_ATTEMPTS_MAX),
            (0, GATE_ATTEMPTS_MIN),
            ("nope", GATE_ATTEMPTS_DEFAULT),
            (None, GATE_ATTEMPTS_DEFAULT),
        ],
    )
    def test_attempts_clamps_and_returns_an_int(
        self, raw: object, expected: int
    ) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            resolved = preflight_gate.resolve_gate_attempts(raw)
        assert resolved == expected
        assert type(resolved) is int


class TestDeprecatedIsGateBroken:
    """The predicate still splits errors exactly where 3.35.0 split them."""

    @pytest.mark.parametrize(
        "exc",
        [
            DependencyUnavailableError(message="dapr down"),
            RateLimitedError(message="429"),
            ResourceExhaustedError(message="no disk"),
            CancelledError(message="cancelled"),
        ],
    )
    def test_plumbing_categories_are_gate_broken(self, exc: AppError) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert preflight_gate._is_gate_broken(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            SourceUnavailableError(message="source down"),
            AuthError(message="bad creds"),
            ValueError("untyped handler crash"),
        ],
    )
    def test_source_and_untyped_failures_are_not(self, exc: BaseException) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert preflight_gate._is_gate_broken(exc) is False

    def test_agrees_with_the_published_category_set(self) -> None:
        """The predicate and the set it was built from cannot drift apart."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            categories = preflight_gate._GATE_BROKEN_CATEGORIES
            broken = preflight_gate._is_gate_broken(
                DependencyUnavailableError(message="x")
            )
        assert FailureCategory.DEPENDENCY_UNAVAILABLE in categories
        assert broken is True
