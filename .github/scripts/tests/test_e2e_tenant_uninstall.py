"""Tests for the `uninstall` subcommand of .github/scripts/e2e_tenant_app.py.

FND-709. The gap this closes was invisible for months precisely because nothing
asserted it: `install` wrote a `releaseChannel: specific` pin into the app's
HelmRelease values and no code path removed it, so the residue accumulated one
pin per connector per tenant and only surfaced when a stale image aged out of the
registry cache and started failing OTHER repos' installs (LM's health check is
namespace-scoped — DISTR-901).

So these assertions are shaped around "what is left on the tenant afterwards",
not around "did the call return 200". Every route LM can answer with is scripted:
the happy path, the two benign non-deployments, the two permanent refusals, and
the two ways a reconcile can leave a pin behind.

The HTTP seam is stubbed at ``TenantClient.request``, reusing the transport stub
from test_e2e_tenant_app.py rather than a second copy — one stub means one place
where "a fragment match on ``/install`` also matches ``/uninstall``" has to be
remembered.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import e2e_tenant_api as api  # noqa: E402
import e2e_tenant_app as app  # noqa: E402
from e2e_tenant_api import Response, TenantApiError, TenantClient  # noqa: E402
from test_e2e_tenant_app import StubRoute, StubTransport, _ok, _wire  # noqa: E402

_TENANT = "https://example-tenant.atlan.test"
_APP_ID = "019d1f6b-6fea-7db3-96d8-e61e159d0351"
_OTHER_APP_ID = "019d1f6b-6fea-7db3-96d8-e61e159d0352"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2E_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("E2E_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv("ATLAN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(app, "mint_oauth_token", lambda *_a, **_k: "stub.jwt.token")


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "base_url": _TENANT,
        "app_ids": _APP_ID,
        "timeout_seconds": 300,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _reply(status: str, code: int, message: str = "", **extra: object) -> Response:
    """LM's envelope: HTTP 200 carrying its own authoritative ``status_code``."""
    return Response(
        status=200,
        body={"status": status, "status_code": code, "message": message, **extra},
    )


# ── The route itself ─────────────────────────────────────────────────────────


def test_uninstall_path_is_the_tenant_scoped_sibling_of_install() -> None:
    """The two are one pair: uninstall must target the same tenant scope install
    does, or a run would install onto `default` and try to clean up elsewhere.

    Asserted as a derivation rather than a second literal, so the paths cannot
    drift apart without this failing.
    """
    assert api.UNINSTALL_PATH == api.INSTALL_PATH.replace("/install", "/uninstall")
    assert "/tenant/default/apps/" in api.UNINSTALL_PATH


# ── The happy path: the pin is gone ──────────────────────────────────────────


def test_a_completed_uninstall_reports_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "POST", "/uninstall", _reply("success", 202, deployment_id="d1")
                ),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                # LM's third step deletes the AtlanAppInstalled record, so info no
                # longer reports a version.
                StubRoute("GET", "/info", Response(status=404, body={})),
            ]
        ),
    )
    outcomes = app.uninstall(_args())

    assert [o.outcome for o in outcomes] == ["removed"]
    assert outcomes[0].cleared is True
    assert outcomes[0].deployment_id == "d1"
    assert transport.paths("POST") == [
        api.UNINSTALL_PATH.format(app_id=_APP_ID)
    ], "the app id must reach the path through the module's own constant"


def test_the_read_back_is_what_decides_and_not_the_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SUCCEEDED uninstall deployment whose install record is still there is
    residue, not success. Same rule as install()'s version read-back: LM's own
    verdict is namespace-scoped, so direct evidence about THIS app decides."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "POST", "/uninstall", _reply("success", 202, deployment_id="d1")
                ),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok({"version_text": "sdr-test-abc12345"})),
            ]
        ),
    )
    outcomes = app.uninstall(_args())

    assert outcomes[0].cleared is False
    assert outcomes[0].outcome == "unreachable"
    assert "sdr-test-abc12345" in outcomes[0].detail


# ── The read-back must fail CLOSED ───────────────────────────────────────────
#
# Same class of bug as the router 400 below, one layer further in: `install`'s
# `_installed_version` folds every non-2xx, every unreadable body and every
# placeholder version into "", because ITS caller reads "" as "install it" and a
# wrong empty costs one redundant install. Reusing it to assert "the pin is gone"
# inverts that: a 500, an expired credential or a dropped connection reads as a
# clean tenant. With `continue-on-error` on the `release-tenant` step, that is
# silent — the pin survives and the next repo's install pays for it.
#
# So only a real 404, or a 200 with no install record, may report `removed`.


@pytest.mark.parametrize("status", [401, 403, 500, 502, 504])
def test_a_failed_read_back_is_residue_and_not_a_clean_tenant(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Any non-404 failure on the info GET proves nothing about the install
    record, so it is residue. A 404 is the only failure that IS evidence."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "POST", "/uninstall", _reply("success", 202, deployment_id="d1")
                ),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", Response(status=status, body={})),
            ]
        ),
    )
    outcome = app.uninstall(_args())[0]

    assert outcome.outcome == "unreachable"
    assert outcome.cleared is False
    assert str(status) in outcome.detail


def test_a_transport_error_on_the_read_back_is_residue_not_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GET is wrapped in the same try/except as the POST: _uninstall_one
    promises never to raise, and a dropped connection while confirming must not
    escape as an exception NOR report the pin cleared."""

    def _explode(
        _self: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> Response:
        if method == "POST":
            return _reply("success", 202, deployment_id="d1")
        if "/deployments/" in path:
            return _ok({"deployment_status": "SUCCEEDED"})
        raise TenantApiError(f"{method} {path} could not reach the tenant")

    monkeypatch.setattr(TenantClient, "request", _explode)
    outcome = app.uninstall(_args())[0]

    assert outcome.outcome == "unreachable"
    assert outcome.cleared is False
    assert "could not read the app's info back" in outcome.detail


def test_an_install_record_with_no_readable_version_is_residue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`version_text: unknown` is a placeholder, not a version — so the version
    read comes back empty while the install record is plainly still there. The
    record's PRESENCE decides, not whether a version could be read out of it."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "POST", "/uninstall", _reply("success", 202, deployment_id="d1")
                ),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute(
                    "GET", "/info", _ok({"installed": {"version_text": "unknown"}})
                ),
            ]
        ),
    )
    outcome = app.uninstall(_args())[0]

    assert outcome.outcome == "unreachable"
    assert outcome.cleared is False
    assert "'installed'" in outcome.detail


def test_a_non_json_read_back_is_residue(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTML error page from a proxy is a 200 whose body is not an object. It
    confirms nothing, so it cannot report the pin cleared."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "POST", "/uninstall", _reply("success", 202, deployment_id="d1")
                ),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute(
                    "GET", "/info", Response(status=200, body="<html>502</html>")
                ),
            ]
        ),
    )
    outcome = app.uninstall(_args())[0]

    assert outcome.outcome == "unreachable"
    assert outcome.cleared is False
    assert "non-JSON" in outcome.detail


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"catalog": {"app_version": {"version": "sdr-test-abc12345"}}},
        {"installed": {}},
        {"deployment": {"deployment_status": "SUCCEEDED"}},
    ],
    ids=["empty", "catalog-only", "empty-record", "deployment-only"],
)
def test_a_200_with_no_install_record_is_removed(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    """The other side of failing closed: these must NOT be reported as residue.

    `catalog` describes the app in general and is present whether or not it is
    installed (`resolve_version_via_catalog` only reads it when an `installed`
    UUID matches), `deployment` may be the uninstall's own reconcile, and an empty
    record carries no install. Reporting any of them as residue would red every
    clean uninstall and train whoever reads the report to ignore it.
    """
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "POST", "/uninstall", _reply("success", 202, deployment_id="d1")
                ),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok(payload)),
            ]
        ),
    )
    outcome = app.uninstall(_args())[0]

    assert outcome.outcome == "removed"
    assert outcome.cleared is True


# ── The router 400 that nearly read as "already clean" ───────────────────────
#
# Found by probing a live tenant, not by reading code, and it is the sharpest
# regression risk in this subcommand: the first implementation keyed
# `not_installed` on install's loose `"not found" in message` match, and Heracles'
# router 400 says "Path was not found". A tenant that does not proxy the route
# therefore reported "not-installed / cleared", green, on every run, forever —
# leaving the pin exactly where it was. The failure mode FND-709 exists to remove,
# reintroduced inside the fix for it.


def _router_400() -> Response:
    """Heracles' verbatim answer for a path it does not proxy.

    Captured live against a real tenant, alongside a control: an invented path
    returns this byte-for-byte, while a route that IS proxied returns LM's
    envelope (HTTP 200, in-body `status_code`, `status: "error"`). So the shape
    below is the router, not LM, and not the app.
    """
    return Response(
        status=400, body={"status_code": 400, "message": "Path was not found"}
    )


def test_an_unproxied_route_is_residue_and_not_a_clean_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        StubTransport(routes=[StubRoute("POST", "/uninstall", _router_400())]),
    )
    outcome = app.uninstall(_args())[0]

    assert outcome.outcome == "route-missing"
    assert (
        outcome.cleared is False
    ), "a tenant that cannot uninstall at all must never report its pin cleared"
    # And the detail has to point at the tenant, not at the app or the request:
    # nothing about either will ever change this answer.
    assert "Heracles" in outcome.detail


def test_not_installed_ignores_the_message_and_keys_on_the_code() -> None:
    """The predicate, directly, because this is where the false green came from.

    LM's real 404 is an enveloped `status_code`, which needs no message match. A
    message-based match cannot tell it from the router's 400.
    """
    router = app._MarketplaceReply.parse(_router_400())
    assert router.route_missing is True
    assert router.not_installed is False

    lm = app._MarketplaceReply.parse(_reply("error", 404, "App with ID '…' not found"))
    assert lm.not_installed is True
    assert lm.route_missing is False

    # A 404 whose message says nothing useful is still a 404.
    assert app._MarketplaceReply.parse(_reply("error", 404, "")).not_installed is True

    # And the loose predicate install retries on must NOT be what drives this:
    # it matches the router 400, which is exactly the bug.
    assert router.not_found is True, (
        "install's not_found is deliberately loose (a false positive costs one "
        "retry). This assertion documents that looseness so nobody re-points "
        "not_installed at it."
    )


# ── Benign non-deployments ───────────────────────────────────────────────────


def test_a_404_is_terminal_success_not_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SAME status code install retries for minutes. On uninstall the tenant
    is already in the state being asked for, so retrying would burn the whole
    budget waiting for something that has already happened."""
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "POST",
                    "/uninstall",
                    _reply("error", 404, "App with ID '…' not found"),
                )
            ]
        ),
    )
    outcomes = app.uninstall(_args())

    assert outcomes[0].outcome == "not-installed"
    assert outcomes[0].cleared is True
    assert len(transport.paths("POST")) == 1, "a 404 must not be retried"
    assert (
        transport.paths("GET") == []
    ), "nothing to poll and nothing to read back: there is no install record"


def test_success_without_a_deployment_still_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LM answering success with no deployment_id is not evidence the pin is gone.
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("POST", "/uninstall", _reply("success", 200, "done")),
                StubRoute("GET", "/info", Response(status=404, body={})),
            ]
        ),
    )
    assert app.uninstall(_args())[0].outcome == "removed"


# ── Permanent refusals ───────────────────────────────────────────────────────


def test_a_system_app_is_refused_and_named_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """409 is permanent, so it must not read as a transient. It is also the one
    outcome that needs a DIFFERENT fix (FND-438: system apps are reconciler-owned
    and this route can never clear their pin), so the detail has to say so."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "POST", "/uninstall", _reply("error", 409, "app is a system app")
                )
            ]
        ),
    )
    outcome = app.uninstall(_args())[0]

    assert outcome.outcome == "refused"
    assert outcome.cleared is False
    assert "system app" in outcome.detail
    assert "FND-438" in outcome.detail


def test_a_non_default_deployment_is_refused_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "POST",
                    "/uninstall",
                    _reply("error", 400, "only the default deployment is supported"),
                )
            ]
        ),
    )
    outcome = app.uninstall(_args())[0]

    assert outcome.outcome == "refused"
    assert "default" in outcome.detail


# ── The two ways a reconcile leaves a pin behind ─────────────────────────────


@pytest.mark.parametrize("terminal", ["FAILED", "STILL_RUNNING"])
def test_an_unconfirmed_deployment_is_residue(
    monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    """A FAILED uninstall and one that never reaches a terminal state are the
    SAME answer here, unlike on the install side. There, a namespace-scoped FAILED
    verdict could be about somebody else's pod and the version read-back could
    overrule it; here there is no read-back that can prove the HelmRelease is
    gone, so both mean "a pin may remain"."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "POST", "/uninstall", _reply("success", 202, deployment_id="d1")
                ),
            ],
            sticky=[
                StubRoute("GET", "/deployments/", _ok({"deployment_status": terminal})),
            ],
        ),
    )
    outcome = app.uninstall(_args(timeout_seconds=0))[0]

    assert outcome.outcome == "unreachable"
    assert outcome.cleared is False


def test_a_transport_error_on_one_app_does_not_abandon_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep's whole value is telling a human what is LEFT on the tenant, so
    one unreachable app must not cost the answer about the other 29."""

    def _explode(
        _self: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> Response:
        if _APP_ID in path:
            raise TenantApiError(f"{method} {path} could not reach the tenant")
        if method == "POST":
            return _reply("error", 404, "not found")
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(TenantClient, "request", _explode)
    outcomes = app.uninstall(_args(app_ids=f"{_APP_ID},{_OTHER_APP_ID}"))

    assert [(o.app_id, o.outcome) for o in outcomes] == [
        (_APP_ID, "unreachable"),
        (_OTHER_APP_ID, "not-installed"),
    ]


# ── The app-id list ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        f"{_APP_ID},{_OTHER_APP_ID}",
        f"{_APP_ID} {_OTHER_APP_ID}",
        f" {_APP_ID}, {_OTHER_APP_ID} ",
        f"{_APP_ID},{_OTHER_APP_ID},{_APP_ID}",
    ],
)
def test_the_app_id_list_is_parsed_and_deduped(raw: str) -> None:
    # A repeated id would uninstall twice, and the second attempt's benign 404
    # would read in the report as though the app had never been installed.
    assert app.resolve_app_ids(raw) == [_APP_ID, _OTHER_APP_ID]


def test_a_non_uuid_app_id_is_rejected_before_any_call() -> None:
    # The ids land in a request path. `str.format` does no escaping, so a value
    # carrying a slash would rewrite the path on a live tenant.
    with pytest.raises(TenantApiError, match="invalid app_id"):
        app.resolve_app_ids(f"{_APP_ID},../../etc/passwd")


def test_an_empty_list_falls_back_to_atlan_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The e2e cleanup passes no ids on purpose: reading atlan.yaml is what makes
    the uninstall's app the SAME app the install's came from."""
    (tmp_path / "atlan.yaml").write_text(f"app_id: {_APP_ID}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert app.resolve_app_ids("") == [_APP_ID]


# ── Exit code and outputs ────────────────────────────────────────────────────


def test_main_exits_zero_when_every_pin_is_cleared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    _wire(
        monkeypatch,
        StubTransport(routes=[StubRoute("POST", "/uninstall", _reply("error", 404))]),
    )

    code = app.main(["uninstall", "--base-url", _TENANT, "--app-ids", _APP_ID])

    assert code == 0
    written = dict(
        line.split("=", 1) for line in output.read_text().splitlines() if line
    )
    assert written["cleared"] == _APP_ID
    assert written["residual"] == ""
    assert written["outcomes"] == f"{_APP_ID}=not-installed"


def test_main_exits_nonzero_on_residue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Residue is a non-zero exit, always. The one caller that must not go red on
    it — the e2e cleanup, in a job whose whole purpose is handing the tenant back
    — says so with `continue-on-error` at its call site, which keeps that
    tolerance visible in the workflow instead of hidden behind a flag here."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("POST", "/uninstall", _reply("error", 409, "system app")),
            ]
        ),
    )

    code = app.main(["uninstall", "--base-url", _TENANT, "--app-ids", _APP_ID])

    assert code == 1
    err = capsys.readouterr().err
    assert "may still carry a version pin" in err
    # The consequence, not just the fact: a pin nobody understands the cost of
    # does not get cleared.
    assert "namespace-scoped" in err


def test_the_residue_report_names_every_residual_app(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[],
            sticky=[
                StubRoute("POST", "/uninstall", _reply("error", 409, "system app"))
            ],
        ),
    )

    app.main(
        ["uninstall", "--base-url", _TENANT, "--app-ids", f"{_APP_ID},{_OTHER_APP_ID}"]
    )

    err = capsys.readouterr().err
    assert "2 of 2" in err
    assert _APP_ID in err
    assert _OTHER_APP_ID in err


def test_the_uninstall_wait_is_shorter_than_the_installs() -> None:
    """It is spent while the tenant LEASE IS STILL HELD, so it is not free the way
    the install's wait is: every second here is a second the next run queues for a
    tenant nobody is using."""
    assert (
        app.DEFAULT_UNINSTALL_TIMEOUT_SECONDS < app.DEFAULT_DEPLOYMENT_TIMEOUT_SECONDS
    )
