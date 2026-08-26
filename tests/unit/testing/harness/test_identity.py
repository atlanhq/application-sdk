"""Unit tests for run-id and unique-name minting, and the tenant env read.

Every one of these was untestable before the extraction: ``setup_method`` read
``time.time`` and ``os.environ`` directly, so the only available assertion about
a minted name was that it looked roughly right. The clock seam is what turns
these into assertions about an exact string — which matters because the minted
qualified name is what teardown purges, and a name a test cannot predict is a
purge a test cannot verify.
"""

from __future__ import annotations

import pytest

from application_sdk.testing.harness import MissingTenantEnvError
from application_sdk.testing.harness.identity import (
    Minter,
    TenantAuth,
    read_tenant_auth,
)


def _minter(
    *, now: int = 1_700_000_000, random: int = 42, run_id_env: str | None = None
) -> Minter:
    """A minter whose every output is a function of its arguments."""
    return Minter(
        clock=lambda: now,
        randbelow=lambda _bound: random,
        run_id_env=run_id_env,
    )


# ---------------------------------------------------------------------------
# run_id
# ---------------------------------------------------------------------------


def test_the_ambient_ci_run_id_wins_over_the_clock() -> None:
    assert _minter(run_id_env="123456").run_id() == 123456


def test_a_non_numeric_run_id_degrades_to_the_clock() -> None:
    """A locally-set GITHUB_RUN_ID=local should not fail a run before it starts:
    the id only ever scopes names."""
    assert _minter(run_id_env="local").run_id() == 1_700_000_000


@pytest.mark.parametrize("ambient", [None, "", "  "])
def test_an_absent_or_blank_run_id_falls_back_to_the_clock(ambient: str | None) -> None:
    assert _minter(run_id_env=ambient).run_id() == 1_700_000_000


# ---------------------------------------------------------------------------
# unique_suffix
# ---------------------------------------------------------------------------


def test_the_suffix_is_the_clock_and_a_zero_padded_random_half() -> None:
    assert _minter(now=1_700_000_000, random=42).unique_suffix() == "1700000000000042"


def test_the_suffix_is_purely_numeric() -> None:
    """Atlas rejects a connection name carrying hyphens or letters."""
    assert _minter(random=999_999).unique_suffix().isdigit()


def test_the_random_half_is_a_fixed_width() -> None:
    """Without the padding the clock half and the random half are not separable
    by eye when reading a tenant's asset list."""
    padded = _minter(now=1, random=7).unique_suffix()
    assert padded == "1000007"


def test_two_runs_in_the_same_second_do_not_collide() -> None:
    """A collision means one run's teardown purges the other run's assets."""
    same_second = 1_700_000_000
    first = _minter(now=same_second, random=1).unique_suffix()
    second = _minter(now=same_second, random=2).unique_suffix()
    assert first != second


def test_the_random_half_is_drawn_below_a_six_digit_bound() -> None:
    """The bound is what makes the padding a padding rather than a truncation."""
    seen: list[int] = []

    def _randbelow(bound: int) -> int:
        seen.append(bound)
        return 0

    Minter(clock=lambda: 0, randbelow=_randbelow).unique_suffix()
    assert seen == [1_000_000]


# ---------------------------------------------------------------------------
# connection_identity
# ---------------------------------------------------------------------------


def test_the_connection_identity_is_predictable_end_to_end() -> None:
    identity = _minter(now=1_700_000_000, random=42).connection_identity("postgres")
    assert identity.qualified_name == "default/postgres/1700000000000042"
    assert identity.display_name == "postgres-1700000000000042"


def test_both_names_come_from_one_suffix() -> None:
    """Minting them separately lets a clock tick between the two, and a display
    name that disagrees with its qualified name is a run nobody can trace."""
    ticks = iter([1, 2, 3, 4])
    identity = Minter(
        clock=lambda: next(ticks), randbelow=lambda _bound: 0
    ).connection_identity("api")
    assert identity.display_name == f"api-{identity.qualified_name.rsplit('/', 1)[1]}"


def test_the_connection_type_is_the_middle_segment() -> None:
    """Connectors whose Atlan catalog type differs from their short name pass the
    catalog type — the qualified name has to carry that, not the app name."""
    identity = _minter().connection_identity("api")
    assert identity.qualified_name.startswith("default/api/")


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------


def test_the_clock_is_an_argument_not_a_patched_global() -> None:
    """Patching time.monotonic process-wide in an async test hands the same mock
    to the asyncio loop's own clock, which surfaces as a flaky StopIteration in
    code that has nothing to do with the clock."""
    early = _minter(now=1).unique_suffix()
    late = _minter(now=2).unique_suffix()
    assert (early, late) == ("1000042", "2000042")


def test_from_environment_reads_the_ci_run_id() -> None:
    minter = Minter.from_environment({"GITHUB_RUN_ID": "987654"})
    assert minter.run_id() == 987654


def test_from_environment_without_a_ci_run_id_mints_from_the_real_clock() -> None:
    minted = Minter.from_environment({}).run_id()
    # 2020-01-01, i.e. any real epoch reading is comfortably past it.
    assert minted > 1_577_836_800


# ---------------------------------------------------------------------------
# read_tenant_auth
# ---------------------------------------------------------------------------


def test_the_tenant_url_loses_its_trailing_slash() -> None:
    """Every downstream f"{base_url}/..." would otherwise produce two."""
    auth = read_tenant_auth(
        {"ATLAN_BASE_URL": "https://tenant.atlan.com/", "ATLAN_API_KEY": "k"}
    )
    assert auth == TenantAuth(base_url="https://tenant.atlan.com", api_key="k")


def test_the_oauth_pair_is_carried_when_present() -> None:
    auth = read_tenant_auth(
        {
            "ATLAN_BASE_URL": "https://tenant.atlan.com",
            "ATLAN_API_KEY": "k",
            "SDR_CLIENT_ID": "id",
            "SDR_CLIENT_SECRET": "secret",
        }
    )
    assert (auth.oauth_client_id, auth.oauth_client_secret) == ("id", "secret")


def test_a_blank_oauth_value_reads_as_absent() -> None:
    """Empty string and unset mean the same thing to the client, and only one of
    them can be passed on."""
    auth = read_tenant_auth(
        {
            "ATLAN_BASE_URL": "https://tenant.atlan.com",
            "ATLAN_API_KEY": "k",
            "SDR_CLIENT_ID": "  ",
        }
    )
    assert auth.oauth_client_id is None


@pytest.mark.parametrize(
    ("environ", "expected_field"),
    [
        ({"ATLAN_API_KEY": "k"}, "ATLAN_BASE_URL"),
        ({"ATLAN_BASE_URL": "https://t"}, "ATLAN_API_KEY"),
        ({"ATLAN_BASE_URL": " ", "ATLAN_API_KEY": "k"}, "ATLAN_BASE_URL"),
        ({}, "ATLAN_BASE_URL,ATLAN_API_KEY"),
    ],
)
def test_a_missing_tenant_variable_names_itself(
    environ: dict[str, str], expected_field: str
) -> None:
    with pytest.raises(MissingTenantEnvError) as caught:
        read_tenant_auth(environ)
    assert caught.value.field == expected_field


def test_the_api_key_requirement_says_why_it_is_not_optional() -> None:
    """An operator who has a working OAuth pair will otherwise read the API key
    as redundant and drop it."""
    with pytest.raises(MissingTenantEnvError) as caught:
        read_tenant_auth({"ATLAN_BASE_URL": "https://t"})
    assert "realm-admin" in str(caught.value)
