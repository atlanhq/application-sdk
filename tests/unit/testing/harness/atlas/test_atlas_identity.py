"""The admin-ACL read and the one Atlas write, lifted out of ``BaseE2ETest``.

Both were synchronous pyatlan calls inside the base class before child H, each
building its own ``AtlanClient`` — which is what the three ``P024`` waivers in
``testing/e2e`` were suppressing. They are here now, on the same async client
every other Atlas call in a run uses.

Two claims are worth pinning beyond "it calls pyatlan".

**A tenant without a ``$admin`` role and an unreadable role cache are different
answers.** The first is a finding about the tenant and settles with an empty
identity; the second is
:class:`~application_sdk.testing.harness.outcome.Indeterminate`. The callers act
on the difference — ``BaseE2ETest`` degrades and lets Atlas reject the create
with a message an operator can read, ``SQLAppE2ETest`` raises — which is only
possible because this function reports rather than decides.

**The qualified name is an input.** ``Connection.creator`` derives it as
``default/<type>/<epoch>``, at one second of resolution, and the lifted code
adopted whatever came back. Two legs of one e2e matrix starting in the same
second therefore shared a connection, and the first to finish purged the
other's assets. ``test_the_minted_qualified_name_is_what_is_created`` is the
regression guard for that.
"""

from __future__ import annotations

from typing import Any

import pytest

from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.atlas import UnknownConnectorTypeError
from application_sdk.testing.harness.outcome import Indeterminate, Settled
from tests.unit.testing._atlas_fakes import FakeAtlasClient, FakeSearchResult

_QN = "default/api/1700000000123456"


def _client(**overrides: Any) -> FakeAtlasClient:
    return FakeAtlasClient(lambda _request: FakeSearchResult(), **overrides)


# ---------------------------------------------------------------------------
# admin_identity
# ---------------------------------------------------------------------------


async def test_a_resolvable_admin_settles_with_both_halves() -> None:
    reading = await atlas.admin_identity(
        _client(roles={"$admin": "role-guid"}, username="svc-account")
    )
    assert isinstance(reading, Settled)
    assert reading.value.roles == ("role-guid",)
    assert reading.value.users == ("svc-account",)


async def test_a_tenant_with_no_admin_role_settles_empty_rather_than_failing() -> None:
    """A missing role is a finding about the tenant, not an unreadable read.

    The distinction is what lets the two callers disagree: the base class
    degrades and lets the create be rejected where an operator can see the ACL,
    while a SQL suite raises because its spec puts the role on unconditionally.
    """
    reading = await atlas.admin_identity(_client(roles={}, username="svc"))
    assert isinstance(reading, Settled)
    assert reading.value.roles == ()
    assert reading.value.users == ("svc",)


async def test_an_unreadable_cache_is_indeterminate_not_an_empty_identity() -> None:
    reading = await atlas.admin_identity(
        _client(identity_error=RuntimeError("role cache is down"))
    )
    assert isinstance(reading, Indeterminate)
    assert isinstance(reading.cause, RuntimeError)


async def test_a_user_with_no_username_contributes_nothing() -> None:
    reading = await atlas.admin_identity(
        _client(roles={"$admin": "role-guid"}, username=None)
    )
    assert isinstance(reading, Settled)
    assert reading.value.users == ()


async def test_the_role_name_is_a_parameter() -> None:
    reading = await atlas.admin_identity(
        _client(roles={"$custom": "other-guid"}), role_name="$custom"
    )
    assert isinstance(reading, Settled)
    assert reading.value.roles == ("other-guid",)


# ---------------------------------------------------------------------------
# create_connection
# ---------------------------------------------------------------------------


async def test_the_minted_qualified_name_is_what_is_created() -> None:
    """The regression guard: the run's own name, not one derived from the clock.

    ``Connection.creator`` builds ``default/<type>/<epoch>``. Two matrix legs
    that start in the same second get the same string, and teardown purges by
    qualified name — so one leg deletes the other's assets and both report a
    clean run.
    """
    client = _client()
    returned = await atlas.create_connection(
        client,
        qualified_name=_QN,
        display_name="api-1700000000123456",
        connector_type="api",
        admin_roles=["role-guid"],
    )
    assert returned == _QN
    saved = client.saved[0]
    assert saved.qualified_name == _QN
    assert saved.name == "api-1700000000123456"


async def test_the_connector_type_sets_the_name_and_category() -> None:
    client = _client()
    await atlas.create_connection(
        client,
        qualified_name=_QN,
        display_name="api-x",
        connector_type="api",
        admin_roles=["role-guid"],
    )
    saved = client.saved[0]
    assert saved.connector_name == "api"
    assert saved.attributes.category is not None


async def test_the_acl_reaches_the_saved_connection() -> None:
    client = _client()
    await atlas.create_connection(
        client,
        qualified_name=_QN,
        display_name="api-x",
        connector_type="api",
        admin_users=["svc"],
        admin_groups=["grp"],
        admin_roles=["role-guid"],
    )
    saved = client.saved[0]
    assert saved.admin_users == {"svc"}
    assert saved.admin_groups == {"grp"}
    assert saved.admin_roles == {"role-guid"}


async def test_every_acl_half_is_validated_before_the_write() -> None:
    """The three checks ``Connection.creator`` performed, kept on the way across.

    They are why a typo in an ACL fails naming the bad value instead of arriving
    as an Atlas rejection naming the whole request.
    """
    client = _client()
    await atlas.create_connection(
        client,
        qualified_name=_QN,
        display_name="api-x",
        connector_type="api",
        admin_users=["svc"],
        admin_groups=["grp"],
        admin_roles=["role-guid"],
    )
    assert client.user_cache.validated == [["svc"]]
    assert client.group_cache.validated == [["grp"]]
    assert client.role_cache.validated == [["role-guid"]]


async def test_a_negative_placeholder_guid_is_assigned() -> None:
    """Atlas assigns the real GUID; a create without a placeholder is rejected.

    ``Connection.creator`` got this from ``@init_guid`` — invisible until it is
    missing, at which point the failure is a server-side rejection that names
    nothing useful.
    """
    client = _client()
    await atlas.create_connection(
        client,
        qualified_name=_QN,
        display_name="api-x",
        connector_type="api",
        admin_roles=["role-guid"],
    )
    assert int(client.saved[0].guid) < 0


async def test_an_unknown_connector_type_names_the_fix() -> None:
    client = _client()
    with pytest.raises(UnknownConnectorTypeError) as exc:
        await atlas.create_connection(
            client,
            qualified_name="default/nope/1",
            display_name="nope-1",
            connector_type="not-a-real-connector",
            admin_roles=["role-guid"],
        )
    assert exc.value.field == "connection_type"
    assert "not-a-real-connector" in str(exc.value)
    assert client.saved == []


async def test_a_rejected_write_propagates_rather_than_becoming_a_verdict() -> None:
    """Unlike every read here, a failed write leaves no degraded mode to report.

    The caller has no connection at all, so there is nothing an ``Outcome``
    could usefully say that the exception does not.
    """
    client = _client()

    async def _refuse(_asset: Any) -> None:
        raise RuntimeError("ATLAS-400-00-114")

    client.asset.save = _refuse  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="ATLAS-400-00-114"):
        await atlas.create_connection(
            client,
            qualified_name=_QN,
            display_name="api-x",
            connector_type="api",
        )
