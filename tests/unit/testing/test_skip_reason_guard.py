"""The ``tests/conftest.py`` hook that fails an unconditional skip naming no ticket.

Run against an inner pytest session so the hook acts on real collection and
setup, not on a hand-built item (FND-4286).
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

_CONFTEST = (
    "from tests.conftest import (  # noqa: F401\n"
    "    pytest_collection_modifyitems,\n"
    "    pytest_runtest_setup,\n"
    ")\n"
)

_TESTS = """
import pytest


@pytest.mark.skip(reason="broken since the refactor")
def test_untracked():
    pass


@pytest.mark.skip
def test_no_reason():
    pass


@pytest.mark.skip(reason="waits on FND-1234")
def test_tracked():
    pass


@pytest.mark.skipif(True, reason="needs a platform this is not")
def test_conditional():
    pass


@pytest.mark.parametrize("x", [pytest.param(1, marks=pytest.mark.skip(reason="later"))])
def test_param(x):
    pass
"""


def test_unticketed_skips_fail_and_ticketed_ones_skip(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(test_inner=_TESTS)

    result = pytester.runpytest("-p", "no:randomly", "-p", "no:xdist", "-rA")

    result.assert_outcomes(errors=3, skipped=2)
    result.stdout.fnmatch_lines(
        [
            "*ERROR*test_untracked - Failed: skip reason*",
            "*ERROR*test_no_reason - Failed: skip reason*",
            "*ERROR*test_param*1* - Failed: skip reason*",
        ]
    )


def test_a_module_level_skip_is_checked(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(
        test_inner=(
            "import pytest\n"
            "pytestmark = pytest.mark.skip(reason='whole module off')\n"
            "def test_one():\n"
            "    pass\n"
        )
    )

    result = pytester.runpytest("-p", "no:randomly", "-p", "no:xdist")

    result.assert_outcomes(errors=1)
