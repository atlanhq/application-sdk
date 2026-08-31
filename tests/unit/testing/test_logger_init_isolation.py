"""The logger-init guard, asserted by running pytest inside pytest.

``restore_logger_init_flags`` is a cross-*test* guard, so nothing about it is
observable from inside a single test: the damage it prevents lands in the
teardown of whatever test happens to run next. ``pytester`` is what makes that
observable — each case below generates a two-test suite whose first test is the
hazard (reset the init flags, never re-initialise) and whose second test is the
victim (install a loguru sink, log through the SDK, remove the sink), then
asserts on the *outcomes* of that inner run.

The suites register the guard the way a consumer would, via ``pytest_plugins``
pointing at the shipped module, so what is under test is the real fixture rather
than a copy of it. See FND-976.
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

# Running pytest inside pytest needs a socket the unit lane's ``--disable-socket``
# otherwise takes away: with no socket the inner run cannot report at all, and
# ``assert_outcomes`` reads an empty summary. Same three-way combination
# ``tests/unit/testing/harness/test_fixtures.py`` documents under FND-961. The
# generated suites are hermetic — they open no real connection.
pytestmark = pytest.mark.enable_socket

#: Reproduces the collision without depending on which SDK call site happens to
#: hold a lazy ``get_logger()`` today. ``storage.upload_file`` is one; the point
#: of the guard is that *any* of them is enough, so the victim calls the accessor
#: directly rather than pinning the test to one incidental caller.
VICTIM = '''
from loguru import logger as loguru_logger

from application_sdk.observability.logger_adaptor import get_logger


def test_hazard():
    """Reset the init flags and never re-initialise — the leak."""
    from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter

    AtlanLoggerAdapter._reset_for_testing()
    assert AtlanLoggerAdapter._initialized is False


def test_victim():
    """Capture logs across a call that may construct an adapter."""
    records = []
    sink_id = loguru_logger.add(lambda m: records.append(m.record), level="DEBUG")
    try:
        get_logger("fnd976.victim").info("hello")
        assert records, "capture sink was removed mid-test"
    finally:
        loguru_logger.remove(sink_id)
'''

WITH_GUARD = 'pytest_plugins = ["application_sdk.testing.fixtures"]\n'
WITHOUT_GUARD = ""


def _run(pytester: pytest.Pytester, conftest: str) -> pytest.RunResult:
    pytester.makeconftest(conftest)
    pytester.makepyfile(test_collision=VICTIM)
    # ``-p no:randomly`` is belt and braces: the two tests must run in the
    # written order for the hazard to reach the victim at all.
    return pytester.runpytest("-p", "no:randomly")


def test_leak_breaks_the_next_test_without_the_guard(
    pytester: pytest.Pytester,
) -> None:
    """The differential: without the fixture, the victim is collateral damage.

    This is the assertion that stops the guard from being a fixture nobody can
    tell is working. If the SDK ever stops taking every sink out on init, this
    case fails and the guard can go.
    """
    result = _run(pytester, WITHOUT_GUARD)

    assert result.ret != 0, (
        "expected the leaked init flags to break the victim test; "
        "if this now passes, AtlanLoggerAdapter.__init__ no longer removes "
        "every loguru sink and the guard is obsolete"
    )
    result.stdout.fnmatch_lines(["*capture sink was removed mid-test*"])


def test_guard_keeps_the_leak_from_reaching_the_next_test(
    pytester: pytest.Pytester,
) -> None:
    """With the fixture registered, the same two tests both pass."""
    result = _run(pytester, WITH_GUARD)

    result.assert_outcomes(passed=2)


def test_guard_never_writes_the_flags_back_down(
    pytester: pytest.Pytester,
) -> None:
    """The mirror hazard: the flag is already down when the guard takes its reading.

    Restoring a snapshot verbatim is only safe when the snapshot is the safe
    value. It is not here: a suite whose flag was lowered by something wider than
    a single test — a session fixture, a conftest, an earlier leak — hands the
    guard a reading of ``False``. If the test then initialises properly, the
    process ends with live sinks and the guard stamps ``False`` back over them,
    handing the next uncached ``get_logger()`` a licence to remove them. That is
    the original bug arrived at from the other direction, so the guard only ever
    restores upward.

    The session-scoped reset is what makes the reading ``False``: higher-scoped
    autouse fixtures set up first, so it lands before the guard takes its
    snapshot. Without it this test passes against the broken guard too — the flag
    would read ``True`` going in and the bad branch would never run.
    """
    pytester.makeconftest(
        WITH_GUARD
        + """
import pytest

from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter


@pytest.fixture(scope="session", autouse=True)
def _lower_the_flag_before_the_guard_reads_it():
    AtlanLoggerAdapter._reset_for_testing()
"""
    )
    pytester.makepyfile(
        test_reinit="""
from loguru import logger as loguru_logger

from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter, get_logger


def test_starts_down_then_initialises():
    assert AtlanLoggerAdapter._initialized is False, "session fixture did not run first"

    AtlanLoggerAdapter("fnd976.reinit")

    assert AtlanLoggerAdapter._initialized is True, "adapter did not initialise"


def test_flag_was_not_stamped_back_down():
    assert (
        AtlanLoggerAdapter._initialized is True
    ), "the guard wrote False back over a completed initialisation"

    records = []
    sink_id = loguru_logger.add(lambda m: records.append(m.record), level="DEBUG")
    try:
        get_logger("fnd976.reinit.victim").info("hello")
        assert records, "capture sink was removed mid-test"
    finally:
        loguru_logger.remove(sink_id)
"""
    )

    pytester.runpytest("-p", "no:randomly").assert_outcomes(passed=2)


def test_guard_restores_both_flags(pytester: pytest.Pytester) -> None:
    """Not just ``_initialized`` — ``_flush_task_started`` gates thread spawning.

    Leaving that one down lets a later real init spawn a second flush thread. The
    assertion is the latch, not the snapshot: a flag that was up before the test
    is up again after it, whatever the test did to it in between.
    """
    pytester.makeconftest(WITH_GUARD)
    pytester.makepyfile(
        test_flags="""
from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter

BEFORE = {}


def test_capture_then_reset():
    BEFORE["initialized"] = AtlanLoggerAdapter._initialized
    BEFORE["flush"] = AtlanLoggerAdapter._flush_task_started
    AtlanLoggerAdapter._reset_for_testing()
    assert AtlanLoggerAdapter._initialized is False
    assert AtlanLoggerAdapter._flush_task_started is False


def test_flags_that_were_up_are_up_again():
    if BEFORE["initialized"]:
        assert AtlanLoggerAdapter._initialized is True
    if BEFORE["flush"]:
        assert AtlanLoggerAdapter._flush_task_started is True
"""
    )

    pytester.runpytest("-p", "no:randomly").assert_outcomes(passed=2)
