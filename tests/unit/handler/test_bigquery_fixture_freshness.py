"""The BigQuery fixture is what the toolkit renders today (FND-4680).

``test_validate_route_credentials.py`` reads forms from
``fixtures/generated_bigquery_credentials/``. Those files are rendered from
``fixtures/bigquery_contract/app.pkl``; this test renders the contract again
and compares bytes, so a toolkit change that alters the forms fails here, with
the command that fixes it, rather than leaving the route tested against a shape
the toolkit no longer emits.

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
from tests.unit.handler import bigquery_fixture


def _pinned_pkl() -> str | None:
    found = shutil.which("pkl")
    if found is None or runtime_version(found) != PKL_VERSION:
        return None
    return found


def test_fixture_matches_the_rendered_contract() -> None:
    pkl = _pinned_pkl()
    if pkl is None:
        reason = f"needs pkl {PKL_VERSION} on PATH"
        if os.environ.get("SDK_TESTS_REQUIRE_PKL"):
            pytest.fail(f"SDK_TESTS_REQUIRE_PKL is set but this test {reason}")
        pytest.skip(reason)

    rendered = bigquery_fixture.render(pkl)

    stale = [
        name
        for name, data in bigquery_fixture.checked_in().items()
        if rendered[name] != data
    ]
    assert not stale, (
        f"{stale} under {bigquery_fixture.FIXTURE.name}/ differ from the toolkit's "
        "render of fixtures/bigquery_contract/app.pkl. Run "
        "`uv run poe regen-test-fixtures` and review the diff."
    )
