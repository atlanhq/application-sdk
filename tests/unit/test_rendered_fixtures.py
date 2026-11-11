"""Each rendered fixture is what the toolkit renders today (FND-4680).

The fixtures in ``tests.regen_fixtures.FIXTURES`` are contracts rendered by
the in-repo toolkit. This test renders each contract again and compares bytes,
so a toolkit change that alters a fixture fails here, with the command that
fixes it, rather than leaving its tests running against a shape the toolkit no
longer emits.

It needs ``pkl`` on ``PATH`` at the SDK pin. Without it the test skips, except
where ``SDK_TESTS_REQUIRE_PKL`` is set: the Linux unit legs install the pin and
set it, so CI cannot pass this test by skipping it.
"""

from __future__ import annotations

import os
import shutil

import pytest

from application_sdk.dev.pkl import runtime_version
from application_sdk.pkl_version import PKL_VERSION
from tests.regen_fixtures import FIXTURES, RenderedFixture, checked_in, render


def _pinned_pkl() -> str | None:
    found = shutil.which("pkl")
    if found is None or runtime_version(found) != PKL_VERSION:
        return None
    return found


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.fixture.name)
def test_fixture_matches_the_rendered_contract(fixture: RenderedFixture) -> None:
    pkl = _pinned_pkl()
    if pkl is None:
        reason = f"needs pkl {PKL_VERSION} on PATH"
        if os.environ.get("SDK_TESTS_REQUIRE_PKL"):
            pytest.fail(f"SDK_TESTS_REQUIRE_PKL is set but this test {reason}")
        pytest.skip(reason)

    rendered = render(fixture, pkl)

    stale = [
        name for name, data in checked_in(fixture).items() if rendered[name] != data
    ]
    assert not stale, (
        f"{stale} under {fixture.fixture.name}/ differ from the toolkit's render "
        f"of {fixture.contract.name}. Run `uv run poe regen-test-fixtures` and "
        "review the diff."
    )


class TestMissingPkl:
    """Without the pinned pkl the check skips, unless CI requires it."""

    @pytest.fixture(autouse=True)
    def _no_pkl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tests.unit.test_rendered_fixtures._pinned_pkl", lambda: None
        )

    def test_required_pkl_fails_instead_of_skipping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SDK_TESTS_REQUIRE_PKL", "1")

        # Catch the skip too: left uncaught, a regression to skipping would
        # report this test as skipped rather than failed.
        with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as caught:
            test_fixture_matches_the_rendered_contract(FIXTURES[0])

        assert caught.type is pytest.fail.Exception
        assert "SDK_TESTS_REQUIRE_PKL" in str(caught.value)

    def test_unrequired_pkl_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SDK_TESTS_REQUIRE_PKL", raising=False)

        with pytest.raises(pytest.skip.Exception, match=PKL_VERSION):
            test_fixture_matches_the_rendered_contract(FIXTURES[0])
