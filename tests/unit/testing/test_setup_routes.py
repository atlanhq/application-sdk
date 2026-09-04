"""Proves the workflow-setup route check actually bites.

The check in :mod:`application_sdk.testing.setup_routes` only runs against a
tenant with the app installed, so on its own there is no way to know its central
assertion would fail on the regression it exists to catch. These tests pin that
offline, on every PR.

The fixtures below are real shapes, not invented ones:

* the passing card is what ``GET /api/service/marketplace/apps`` returns for a
  live connector today (``id: "clickhouse-crawler"``, ``name: "clickhouse"``,
  ``entrypoint: "crawler"``)
* the failing card carries ``id: "atlan-clickhouse"`` — the id the build with
  ``packageId`` set produced, observed as a 404'ing
  ``/workflows/setup/atlan-clickhouse`` in a tenant UI

A regression test that does not fail without the fix is not a regression test,
so :func:`test_bites_on_the_packageid_regression` and its miner twin are the
load-bearing ones here: stubbing ``route_mismatch`` to return ``None`` must red
this file.

:class:`TestServedFormRoundTrip` is the other load-bearing part, and it tests
something no amount of shape-fixture assertion can: that
:func:`~application_sdk.testing.setup_routes.served_inputs` really inverts the
envelope the SDK's own configmap endpoint builds. It drives the live FastAPI
route rather than a hand-written dict, so the two sides of the join are pinned
against each other in-repo — the committed generated file and the response a
tenant proxies. The nesting differs by exactly one level between them
(``handler.service`` serves ``raw.get("config", raw)``), which is the easiest
thing in this whole check to get quietly wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing.setup_routes import (
    Card,
    Entrypoint,
    RouteCheckSkipped,
    SetupRouteError,
    declared_inputs,
    input_shortfall,
    locate_cards,
    read_app_name,
    read_entrypoint_names,
    read_entrypoints,
    route_mismatch,
    served_inputs,
    verify,
)

# ---------------------------------------------------------------------------
# Fixtures — the real shapes
# ---------------------------------------------------------------------------


def _card_payload(
    card_id: str, *, entrypoint: str = "crawler", name: str = "clickhouse"
) -> dict[str, Any]:
    """A marketplace card as the catalog returns it, trimmed to the read fields.

    The untrimmed card carries ~45 keys; these are the three the check reads
    plus two that must be *ignored* (``package_id`` and ``installed``), so a
    future reader can see they are deliberately not part of the decision.
    """
    return {
        "id": card_id,
        "name": name,
        "displayName": "ClickHouse Assets",
        "type": "connector",
        "entrypoint": entrypoint,
        "isNewApp": True,
        "execution_mode": "native",
        "installed": True,
        "package_id": "",
        "version": "v0.3.1",
    }


def _workflow_config(config_id: str = "clickhouse-crawler") -> dict[str, Any]:
    """A generated workflow config, trimmed to the read fields.

    ``config`` carries ``steps`` and ``anyOf`` alongside ``properties`` on a
    real one; both are included here precisely so a test proves they are NOT
    read as inputs.
    """
    return {
        "id": config_id,
        "name": "ClickHouse Assets",
        "logo": "https://assets.example.invalid/logo.svg",
        "config": {
            "properties": {
                "extraction-method": {"type": "conditional"},
                "credential-guid": {"type": "string"},
                "connection": {"type": "string"},
            },
            "steps": [{"id": "credentials"}],
            "anyOf": [{"properties": {}}],
        },
    }


def _eps(*names: str) -> list[Entrypoint]:
    """Named entrypoints, as a bundle's `atlan.yaml` declares them."""
    return [Entrypoint(name=name, config_id=f"clickhouse-{name}") for name in names]


def _sole(config_id: str) -> list[Entrypoint]:
    """The single entry point of an app declaring no `entrypoints[]`.

    Labelled by its committed config id — never by an empty string, which is
    the magic value this module deliberately has no place for.
    """
    return [Entrypoint(name=config_id, config_id=config_id, is_sole=True)]


def _configmap_response(
    schema: dict[str, Any], name: str = "clickhouse-crawler"
) -> dict[str, Any]:
    """The ConfigMap a tenant serves: ``data.config`` is a JSON *string*."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name},
        "data": {"config": json.dumps(schema)},
    }


# ---------------------------------------------------------------------------
# route_mismatch — the load-bearing check
# ---------------------------------------------------------------------------


class TestRouteMismatch:
    def test_passes_on_the_shipped_card_shape(self) -> None:
        """The real card and the real generated config agree, so nothing fires."""
        card = Card.from_payload(_card_payload("clickhouse-crawler"))
        assert route_mismatch("crawler", card, "clickhouse-crawler") is None

    def test_bites_on_the_packageid_regression(self) -> None:
        """The FND-1593 shape must be reported, or the check is decoration.

        ``packageId = "@atlan/clickhouse"`` moved the card id to
        ``atlan-clickhouse`` while the workflow config stayed
        ``clickhouse-crawler``, and both setup pages 404'd. This is that exact
        divergence.
        """
        card = Card.from_payload(_card_payload("atlan-clickhouse"))

        reason = route_mismatch("crawler", card, "clickhouse-crawler")

        assert reason is not None, (
            "route_mismatch accepted a card whose id does not match the "
            "generated workflow config id — the shape that 404'd both setup "
            "pages. The check built on it cannot catch that regression."
        )
        # Both sides, or whoever hits this in CI cannot act on it.
        assert "atlan-clickhouse" in reason
        assert "clickhouse-crawler" in reason
        # And the route the user would actually land on.
        assert "/workflows/setup/atlan-clickhouse" in reason

    def test_bites_on_the_miner_regression(self) -> None:
        """The second entrypoint regressed identically and must report identically."""
        card = Card.from_payload(
            _card_payload("atlan-clickhouse-miner", entrypoint="miner")
        )

        reason = route_mismatch("miner", card, "clickhouse-miner")

        assert reason is not None
        assert "atlan-clickhouse-miner" in reason
        assert "clickhouse-miner" in reason

    def test_names_the_cause_so_the_reader_can_act(self) -> None:
        """The message must point at packageId, the known cause (FND-1659)."""
        card = Card.from_payload(_card_payload("atlan-clickhouse"))

        reason = route_mismatch("crawler", card, "clickhouse-crawler")

        assert reason is not None
        assert "packageId" in reason

    def test_reports_a_card_with_no_id(self) -> None:
        """No id at all means the marketplace cannot build a setup link."""
        payload = _card_payload("clickhouse-crawler")
        del payload["id"]

        reason = route_mismatch(
            "crawler", Card.from_payload(payload), "clickhouse-crawler"
        )

        assert reason is not None
        assert "no id" in reason

    def test_reports_an_ungenerated_workflow_config(self) -> None:
        """An id-less config means the artifacts were never generated."""
        card = Card.from_payload(_card_payload("clickhouse-crawler"))

        reason = route_mismatch("crawler", card, "")

        assert reason is not None
        assert "Regenerate the contract" in reason

    def test_a_sole_entrypoint_is_named_by_its_config_id(self) -> None:
        """Never an empty quote in prose, because the name is never empty.

        An earlier revision rendered a flat app as `''` and papered over it with
        a `<flat>` placeholder at every message site. The label is now a
        committed fact, so there is nothing to paper over.
        """
        card = Card.from_payload(_card_payload("atlan-mysql", entrypoint=""))

        reason = route_mismatch("mysql", card, "mysql")

        assert reason is not None
        assert "'mysql'" in reason
        assert "<flat>" not in reason


# ---------------------------------------------------------------------------
# input_shortfall — the subset check
# ---------------------------------------------------------------------------


class TestInputShortfall:
    def test_extra_served_fields_are_not_a_failure(self) -> None:
        """A subset check: the platform may decorate the schema.

        Failing on platform-added fields buys no safety and would red the fleet
        the first time the platform grew one.
        """
        declared = frozenset({"credential-guid", "connection"})
        served = declared | {"labFlag", "platform-injected"}

        assert input_shortfall("crawler", declared, served) is None

    def test_bites_on_a_missing_declared_input(self) -> None:
        """A missing input is the signal — a stale image, or a change that never landed."""
        declared = frozenset({"credential-guid", "connection", "include-filter"})
        served = frozenset({"credential-guid", "connection"})

        reason = input_shortfall("crawler", declared, served)

        assert reason is not None
        assert "include-filter" in reason
        # The served set has to be in the message, or the reader cannot tell a
        # stale image from a renamed field.
        assert "credential-guid" in reason

    def test_a_contract_declaring_nothing_is_reported(self) -> None:
        """Zero declared inputs makes the check vacuous, so it must not pass."""
        reason = input_shortfall("crawler", frozenset(), frozenset({"anything"}))

        assert reason is not None
        assert "declares no inputs" in reason


# ---------------------------------------------------------------------------
# locate_cards — the bug that makes a wrong answer look plausible
# ---------------------------------------------------------------------------


class TestLocateCards:
    def test_entrypoint_alone_is_not_app_scoped(self) -> None:
        """Every connector's crawler card carries ``entrypoint: "crawler"``.

        Filtering on ``entrypoint`` alone matched a dozen unrelated apps when
        the prototype tried it, and the count was plausible enough to look like
        success. The match must AND with the app name.
        """
        # Ours FIRST, foreign cards after. Ordering is load-bearing: the cards
        # are collapsed into a dict keyed by entrypoint, so last-wins. With ours
        # last, a name-blind match would still happen to land on the right card
        # and this test would pass while asserting nothing.
        catalog = [_card_payload("clickhouse-crawler", name="clickhouse")]
        catalog += [
            _card_payload(f"{source}-crawler", name=source)
            for source in (
                "mssql",
                "bigquery",
                "snowflake",
                "postgres",
                "redshift",
                "databricks",
            )
        ]

        cards, reason = locate_cards("clickhouse", _eps("crawler"), catalog)

        assert reason is None
        assert set(cards) == {"crawler"}
        assert cards["crawler"].id == "clickhouse-crawler"

    def test_reports_an_app_absent_from_the_catalog(self) -> None:
        catalog = [_card_payload("mssql-crawler", name="mssql")]

        cards, reason = locate_cards("clickhouse", _eps("crawler"), catalog)

        assert cards == {}
        assert reason is not None
        assert "not installed on this tenant" in reason
        # The catalog size proves the read worked, which distinguishes "not
        # installed" from "token cannot read the catalog".
        assert "1 apps" in reason

    def test_reports_an_entrypoint_with_no_card(self) -> None:
        """An entrypoint with no card has no setup page at all."""
        catalog = [_card_payload("clickhouse-crawler", name="clickhouse")]

        cards, reason = locate_cards("clickhouse", _eps("crawler", "miner"), catalog)

        assert set(cards) == {"crawler"}
        assert reason is not None
        assert "miner" in reason

    def test_a_sole_contract_card_keys_under_its_config_id(self) -> None:
        """No empty-string key anywhere, and the card matches by count.

        The card's own `entrypoint` takes no part in the match — there is one
        card and one form, so there is nothing to choose between.
        """
        catalog = [_card_payload("mysql", name="mysql", entrypoint="")]

        cards, reason = locate_cards("mysql", _sole("mysql"), catalog)

        assert reason is None
        assert cards["mysql"].id == "mysql"


# ---------------------------------------------------------------------------
# served_inputs — the one-level nesting difference
# ---------------------------------------------------------------------------


class TestServedInputs:
    def test_unwraps_the_json_string_config(self) -> None:
        response = _configmap_response(
            {"properties": {"credential-guid": {}, "connection": {}}}
        )

        assert served_inputs(response) == frozenset({"credential-guid", "connection"})

    def test_rejects_a_non_string_config(self) -> None:
        """A nested object instead of a string means the shape changed.

        Silently coping would make the subset check read an empty served set
        and report every declared input as missing.
        """
        response = _configmap_response({"properties": {}})
        response["data"] = {"config": {"properties": {}}}

        with pytest.raises(SetupRouteError, match="no string data.config"):
            served_inputs(response)

    def test_rejects_a_response_with_no_data(self) -> None:
        with pytest.raises(SetupRouteError, match="no 'data' object"):
            served_inputs({"metadata": {"name": "x"}})

    def test_rejects_unparseable_config(self) -> None:
        response = _configmap_response({"properties": {}})
        response["data"] = {"config": "{not json"}

        with pytest.raises(SetupRouteError, match="not valid JSON"):
            served_inputs(response)

    def test_a_schema_with_no_properties_serves_nothing(self) -> None:
        """Empty, not an error — ``input_shortfall`` is what reports the gap."""
        assert served_inputs(_configmap_response({"steps": []})) == frozenset()


class TestDeclaredInputs:
    def test_reads_only_config_properties(self) -> None:
        """``steps`` and ``anyOf`` are the wizard and the visibility rules."""
        assert declared_inputs(_workflow_config()) == frozenset(
            {"extraction-method", "credential-guid", "connection"}
        )

    def test_a_config_less_payload_declares_nothing(self) -> None:
        assert declared_inputs({"id": "x"}) == frozenset()


# ---------------------------------------------------------------------------
# The artifact readers
# ---------------------------------------------------------------------------


def _write_bundle(root: Path) -> None:
    """A two-entrypoint bundle, laid out exactly as the toolkit emits one."""
    (root / "atlan.yaml").write_text(
        "name: clickhouse\n"
        "app_id: 019c72eb-f048-7a40-bb42-e5689dd8c150\n"
        "entrypoints:\n"
        "- name: crawler\n"
        "  display_name: ClickHouse Assets\n"
        "  type: connector\n"
        "- name: miner\n"
        "  display_name: ClickHouse Miner\n"
        "  type: miner\n"
    )
    generated = root / "app" / "generated"
    # The credential template the toolkit hoists to the bundle root. Its real
    # shape carries a `config` and NO top-level `id` — verified against
    # atlan-clickhouse-app and atlan-mysql-app.
    (generated).mkdir(parents=True)
    (generated / "atlan-connectors-clickhouse.json").write_text(
        json.dumps({"connector": "clickhouse", "config": {"properties": {}}})
    )
    for entrypoint, config_id in (
        ("crawler", "clickhouse-crawler"),
        ("miner", "clickhouse-miner"),
    ):
        directory = generated / entrypoint
        directory.mkdir()
        (directory / "manifest.json").write_text("{}")
        (directory / f"{config_id}.json").write_text(
            json.dumps(_workflow_config(config_id))
        )


def _write_flat(root: Path) -> None:
    """A single-generated-contract app: everything at the generated root."""
    (root / "atlan.yaml").write_text(
        "name: mysql\napp_id: 019c72eb-f048-7a40-bb42-e5689dd8c151\n"
    )
    generated = root / "app" / "generated"
    generated.mkdir(parents=True)
    (generated / "manifest.json").write_text("{}")
    # Sorts BEFORE `mysql.json`, which is the whole point — see the test.
    # Real shape: a `config`, no top-level `id`.
    (generated / "atlan-connectors-mysql.json").write_text(
        json.dumps({"connector": "mysql", "config": {"properties": {"host": {}}}})
    )
    (generated / "mysql.json").write_text(json.dumps(_workflow_config("mysql")))


class TestArtifactReaders:
    def test_reads_a_bundle(self, tmp_path: Path) -> None:
        _write_bundle(tmp_path)

        found = read_entrypoints(tmp_path)

        assert [e.name for e in found] == ["crawler", "miner"]
        assert [e.config_id for e in found] == [
            "clickhouse-crawler",
            "clickhouse-miner",
        ]
        assert found[0].declared == frozenset(
            {"extraction-method", "credential-guid", "connection"}
        )

    def test_a_flat_app_does_not_pick_the_credential_template(
        self, tmp_path: Path
    ) -> None:
        """Exclusion by stem, not by hoping the template lacks a key.

        ``atlan-connectors-mysql.json`` sorts alphabetically BEFORE
        ``mysql.json``, so file selection in a flat tree has to reject it
        explicitly rather than relying on ordering.

        Today's credential templates carry a ``config`` and no top-level
        ``id``, so a selector keyed on "has both ``id`` and ``config``" also
        skips them — but only incidentally. Nothing promises a template will
        never gain an ``id``, and if one did, that selector would silently
        start comparing a marketplace card against a credential id.

        ``is_form_configmap`` rejects it by stem, which is the same rule the
        configmap endpoint applies — so the server and this check cannot
        disagree about which file is the form.
        """
        _write_flat(tmp_path)

        found = read_entrypoints(tmp_path)

        assert [e.name for e in found] == ["mysql"]
        assert found[0].config_id == "mysql"
        assert found[0].is_sole is True

    def test_skips_when_nothing_is_generated(self, tmp_path: Path) -> None:
        """Skip, not fail: no generated tree means no setup page to check."""
        (tmp_path / "atlan.yaml").write_text("name: nothing\n")
        (tmp_path / "app" / "generated").mkdir(parents=True)

        with pytest.raises(RouteCheckSkipped, match="no generated contract"):
            read_entrypoints(tmp_path)

    def test_skips_when_the_generated_dir_is_absent(self, tmp_path: Path) -> None:
        (tmp_path / "atlan.yaml").write_text("name: nothing\n")

        with pytest.raises(RouteCheckSkipped):
            read_entrypoints(tmp_path)

    def test_fails_when_a_declared_entrypoint_has_no_config(
        self, tmp_path: Path
    ) -> None:
        """Incoherent committed artifacts are a real failure, not a skip."""
        _write_bundle(tmp_path)
        (tmp_path / "app" / "generated" / "miner" / "clickhouse-miner.json").unlink()

        with pytest.raises(SetupRouteError, match="no setup-form configmap"):
            read_entrypoints(tmp_path)

    def test_fails_when_a_config_has_no_id(self, tmp_path: Path) -> None:
        _write_bundle(tmp_path)
        path = tmp_path / "app" / "generated" / "crawler" / "clickhouse-crawler.json"
        path.write_text(json.dumps({"config": {"properties": {"a": {}}}}))

        with pytest.raises(SetupRouteError, match="no top-level 'id'"):
            read_entrypoints(tmp_path)

    def test_fails_when_a_bundle_declares_no_entrypoints(self, tmp_path: Path) -> None:
        """A nested tree with no atlan.yaml entrypoints has uncheckable cards."""
        _write_bundle(tmp_path)
        (tmp_path / "atlan.yaml").write_text("name: clickhouse\n")

        with pytest.raises(SetupRouteError, match="declares no entrypoints"):
            read_entrypoints(tmp_path)

    def test_reads_the_app_name_lowercased(self, tmp_path: Path) -> None:
        (tmp_path / "atlan.yaml").write_text("name: ClickHouse\n")

        assert read_app_name(tmp_path) == "clickhouse"

    def test_a_missing_atlan_yaml_is_a_clear_error(self, tmp_path: Path) -> None:
        with pytest.raises(SetupRouteError, match="cannot read"):
            read_app_name(tmp_path)

    def test_a_nameless_atlan_yaml_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "atlan.yaml").write_text("app_id: x\n")

        with pytest.raises(SetupRouteError, match='no top-level "name"'):
            read_app_name(tmp_path)

    def test_entrypoint_names_are_empty_for_a_flat_app(self, tmp_path: Path) -> None:
        _write_flat(tmp_path)

        assert read_entrypoint_names(tmp_path) == []


# ---------------------------------------------------------------------------
# The round trip — the two sides of the join, pinned against each other
# ---------------------------------------------------------------------------


class TestServedFormRoundTrip:
    """Does ``served_inputs`` really invert what the SDK's endpoint serves?

    Every other test here feeds hand-written shapes. These drive the live
    ``GET /workflows/v1/configmap/{id}`` route — the endpoint a tenant's
    ``/api/service/configmaps/<name>`` proxies to — so the committed file and
    the served response are compared through the real code on both sides.

    Heracles returns the ConfigMap at the top level while the SDK wraps it in
    its standard ``{"data": ...}`` envelope, so these tests unwrap that one
    layer to stand where the check stands. That unwrapping is Heracles', and
    it is the one part of the path this repo cannot pin.
    """

    @staticmethod
    def _serve(tmp_path: Path, config_id: str) -> dict[str, Any]:
        from application_sdk.handler import service as svc_module
        from tests.unit.handler.test_service import _make_client

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path / "app" / "generated"
        try:
            response = _make_client().get(f"/workflows/v1/configmap/{config_id}")
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original
        assert response.status_code == 200, response.text
        # Peel the SDK's response envelope, which is what Heracles does before
        # handing the bare ConfigMap to the frontend.
        return response.json()["data"]

    def test_the_served_form_declares_everything_the_contract_does(
        self, tmp_path: Path
    ) -> None:
        """The whole check, end to end, through the real endpoint.

        If ``served_inputs`` read one nesting level too deep or too shallow,
        this fails — which is exactly the mistake the differing depths invite.
        """
        _write_bundle(tmp_path)
        committed = json.loads(
            (
                tmp_path / "app" / "generated" / "crawler" / "clickhouse-crawler.json"
            ).read_text()
        )

        body = self._serve(tmp_path, "clickhouse-crawler")

        assert body["metadata"]["name"] == "clickhouse-crawler"
        assert (
            input_shortfall("crawler", declared_inputs(committed), served_inputs(body))
            is None
        )

    def test_a_form_missing_a_contract_input_is_caught(self, tmp_path: Path) -> None:
        """A genuinely stale served schema must fail, not just a version gap.

        The prototype's subset check passed against a tenant one patch version
        behind — but only because that migration produced byte-identical
        configs, so it never demonstrated it could catch a real shortfall. This
        serves a form with an input deleted and proves it does.
        """
        _write_bundle(tmp_path)
        path = tmp_path / "app" / "generated" / "crawler" / "clickhouse-crawler.json"
        committed = json.loads(path.read_text())
        declared = declared_inputs(committed)

        # The deployed image serves a form that predates `include-filter` being
        # renamed in — i.e. one input short of the committed contract.
        stale = json.loads(path.read_text())
        del stale["config"]["properties"]["connection"]
        path.write_text(json.dumps(stale))

        body = self._serve(tmp_path, "clickhouse-crawler")
        reason = input_shortfall("crawler", declared, served_inputs(body))

        assert reason is not None
        assert "connection" in reason

    def test_the_endpoint_rejects_a_name_it_does_not_know(self, tmp_path: Path) -> None:
        """The negative control's premise, pinned against the real endpoint.

        Every 200 the check trusts is worthless if the endpoint answers 200 for
        unknown names. The live check asserts this against the tenant; this
        asserts the SDK server it proxies to behaves that way at all.
        """
        from application_sdk.handler import service as svc_module
        from tests.unit.handler.test_service import _make_client

        _write_bundle(tmp_path)
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path / "app" / "generated"
        try:
            response = _make_client().get(
                "/workflows/v1/configmap/clickhouse-nonexistent-setup-route-check"
            )
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

        assert response.status_code in (400, 403, 404)


# ---------------------------------------------------------------------------
# Entrypoint dataclass
# ---------------------------------------------------------------------------


def test_entrypoint_defaults_are_inert() -> None:
    """A bare Entrypoint declares nothing and names no source."""
    entrypoint = Entrypoint(name="crawler", config_id="clickhouse-crawler")

    assert entrypoint.declared == frozenset()
    assert entrypoint.source is None


# ---------------------------------------------------------------------------
# verify() — the negative control and the bounded poll
# ---------------------------------------------------------------------------


class _FakeRoutes:
    """Stands in for :class:`TenantRoutes` with scripted responses.

    Not a mock: the tests below assert on *sequencing* — how many catalog reads
    happened, and that the bogus-name probe happened before any real fetch — so
    the fake records calls rather than only answering them.
    """

    def __init__(
        self,
        catalogs: list[list[dict[str, Any]]],
        configmaps: dict[str, tuple[int, dict[str, Any]]],
        *,
        unknown_status: int = 404,
    ) -> None:
        self._catalogs = catalogs
        self._configmaps = configmaps
        self._unknown_status = unknown_status
        self.catalog_reads = 0
        self.asked: list[str] = []

    def catalog(self) -> list[dict[str, Any]]:
        index = min(self.catalog_reads, len(self._catalogs) - 1)
        self.catalog_reads += 1
        return self._catalogs[index]

    def configmap(self, name: str) -> tuple[int, dict[str, Any]]:
        self.asked.append(name)
        if name in self._configmaps:
            return self._configmaps[name]
        return self._unknown_status, {}


def _healthy_configmaps() -> dict[str, tuple[int, dict[str, Any]]]:
    schema = {
        "properties": {
            "extraction-method": {},
            "credential-guid": {},
            "connection": {},
            # A platform-added field the contract never named, so the subset
            # check's tolerance is exercised on the happy path too.
            "labFlag": {},
        }
    }
    return {
        "clickhouse-crawler": (200, _configmap_response(schema, "clickhouse-crawler")),
        "clickhouse-miner": (200, _configmap_response(schema, "clickhouse-miner")),
    }


def _both_cards() -> list[dict[str, Any]]:
    return [
        _card_payload("clickhouse-crawler", entrypoint="crawler"),
        _card_payload("clickhouse-miner", entrypoint="miner"),
    ]


class TestVerify:
    def test_passes_end_to_end_on_a_healthy_tenant(self, tmp_path: Path) -> None:
        _write_bundle(tmp_path)
        routes = _FakeRoutes([_both_cards()], _healthy_configmaps())

        report = verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

        assert len(report) == 2
        assert all("resolves" in line for line in report)

    def test_the_negative_control_runs_before_anything_is_trusted(
        self, tmp_path: Path
    ) -> None:
        """The bogus probe must be the FIRST configmap asked for.

        Every 200 the check trusts is worthless if the endpoint answers 200 for
        unknown names, so the discrimination has to be proven before the first
        real fetch — not after, where a vacuous pass has already been reported.
        """
        _write_bundle(tmp_path)
        routes = _FakeRoutes([_both_cards()], _healthy_configmaps())

        verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

        assert routes.asked[0] == "clickhouse-nonexistent-setup-route-check"

    def test_fails_when_unknown_names_are_not_rejected(self, tmp_path: Path) -> None:
        """A 200 for a name that cannot exist makes every assertion vacuous."""
        _write_bundle(tmp_path)
        routes = _FakeRoutes([_both_cards()], _healthy_configmaps(), unknown_status=200)

        with pytest.raises(SetupRouteError, match="proves anything"):
            verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

    def test_reports_the_packageid_regression_against_a_tenant(
        self, tmp_path: Path
    ) -> None:
        """The whole point, exercised through verify() rather than the helper.

        The card ids are the ones the FND-1593 build produced. Note the
        configmaps for the CORRECT names still answer 200 — which is exactly why
        a check asserting ``configmaps/clickhouse-crawler == 200`` would have
        passed straight through this.
        """
        _write_bundle(tmp_path)
        catalog = [
            _card_payload("atlan-clickhouse", entrypoint="crawler"),
            _card_payload("atlan-clickhouse-miner", entrypoint="miner"),
        ]
        routes = _FakeRoutes([catalog], _healthy_configmaps())

        with pytest.raises(SetupRouteError) as caught:
            verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

        message = str(caught.value)
        # Both entrypoints broke, so both must be reported — not just the first.
        assert "atlan-clickhouse" in message
        assert "atlan-clickhouse-miner" in message
        assert "clickhouse-crawler" in message
        assert "clickhouse-miner" in message

    def test_fails_when_the_card_id_resolves_to_a_404(self, tmp_path: Path) -> None:
        """Card and config agree, but the tenant serves no form for the name."""
        _write_bundle(tmp_path)
        configmaps = _healthy_configmaps()
        del configmaps["clickhouse-miner"]
        routes = _FakeRoutes([_both_cards()], configmaps)

        with pytest.raises(SetupRouteError, match="404'd setup page"):
            verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

    def test_fails_when_the_endpoint_serves_a_different_name(
        self, tmp_path: Path
    ) -> None:
        """Asked for one form, served another — the page renders the wrong one."""
        _write_bundle(tmp_path)
        configmaps = _healthy_configmaps()
        configmaps["clickhouse-crawler"] = (
            200,
            _configmap_response(
                {"properties": {"extraction-method": {}}}, "something-else"
            ),
        )
        routes = _FakeRoutes([_both_cards()], configmaps)

        with pytest.raises(SetupRouteError, match="served 'something-else'"):
            verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

    def test_polls_until_the_catalog_reconciles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A card absent on the first read but present later must pass.

        ``install()`` polling the DEPLOYMENT to SUCCEEDED is not evidence that
        LM's catalog snapshot has caught up, and the prototype this generalises
        never exercised the immediately-post-install path — so a single read
        would be flaky-by-construction on exactly the path CI takes.
        """
        from application_sdk.testing import setup_routes as module

        _write_bundle(tmp_path)
        both = _both_cards()
        # First read: only the crawler card has appeared. Second: both.
        routes = _FakeRoutes([both[:1], both], _healthy_configmaps())

        # 0 so the test does not actually sleep. The interval is not what is
        # under test — that a second read HAPPENS is.
        monkeypatch.setattr(module, "_CATALOG_POLL_SECONDS", 0)
        progress: list[str] = []

        report = module.verify(
            tmp_path,
            routes,  # type: ignore[arg-type]
            wait_seconds=60,
            on_progress=progress.append,
        )

        assert len(report) == 2
        assert routes.catalog_reads == 2
        # The wait has to be visible in the log, or a patient step looks hung.
        assert progress and "catalog not ready" in progress[0]

    def test_gives_up_with_the_latest_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A card that never appears fails, naming what was last seen.

        The LAST reason, not the first: an early read that saw no card at all is
        less informative than a late one that saw all but one.
        """
        from application_sdk.testing import setup_routes as module

        _write_bundle(tmp_path)
        routes = _FakeRoutes(
            [[_card_payload("clickhouse-crawler", entrypoint="crawler")]],
            _healthy_configmaps(),
        )
        monkeypatch.setattr(module, "_CATALOG_POLL_SECONDS", 0)

        with pytest.raises(SetupRouteError) as caught:
            module.verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

        message = str(caught.value)
        assert "miner" in message
        assert "Waited 0s" in message

    def test_skips_an_app_with_no_generated_contract(self, tmp_path: Path) -> None:
        """Skip-not-fail, or this is a fleet-wide false positive on day one."""
        (tmp_path / "atlan.yaml").write_text("name: behind-the-scenes\n")
        routes = _FakeRoutes([[]], {})

        with pytest.raises(RouteCheckSkipped):
            verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

        # And it must skip BEFORE touching the tenant: an app with no setup page
        # should cost no tenant calls at all.
        assert routes.catalog_reads == 0
        assert routes.asked == []


# ---------------------------------------------------------------------------
# The two silent-wrong-answer shapes
# ---------------------------------------------------------------------------


class TestCardlessEntrypoints:
    """`marketplace_card: false` is the behind-the-scenes pattern.

    An entry point the DAG invokes with no card for a user to click has no setup
    page, so asserting one exists is a false positive rather than a finding.
    """

    @staticmethod
    def _bundle_with_cardless_miner(root: Path) -> None:
        _write_bundle(root)
        (root / "atlan.yaml").write_text(
            "name: clickhouse\n"
            "entrypoints:\n"
            "- name: crawler\n"
            "  type: connector\n"
            "- name: miner\n"
            "  type: miner\n"
            "  marketplace_card: false\n"
        )

    def test_a_cardless_entrypoint_is_not_checked(self, tmp_path: Path) -> None:
        self._bundle_with_cardless_miner(tmp_path)

        found = read_entrypoints(tmp_path)

        assert [e.name for e in found] == ["crawler"]

    def test_a_cardless_entrypoint_does_not_fail_the_run(self, tmp_path: Path) -> None:
        """The tenant lists no miner card, and that must be fine."""
        self._bundle_with_cardless_miner(tmp_path)
        catalog = [_card_payload("clickhouse-crawler", entrypoint="crawler")]
        routes = _FakeRoutes([catalog], _healthy_configmaps())

        report = verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

        assert len(report) == 1

    def test_absent_means_a_card_is_expected(self, tmp_path: Path) -> None:
        """Absence must NOT be read as "no card".

        Live connectors are marketplace cards today while emitting neither
        `marketplace_card` nor `package_id` — the key only appears once
        `packageId` is set. Keying on absence would skip the entire fleet and
        this check would assert nothing at all, which is strictly worse than
        having no check.
        """
        _write_bundle(tmp_path)  # no marketplace_card key anywhere

        assert read_entrypoint_names(tmp_path) == ["crawler", "miner"]

    def test_true_is_also_checked(self, tmp_path: Path) -> None:
        _write_bundle(tmp_path)
        (tmp_path / "atlan.yaml").write_text(
            "name: clickhouse\n"
            "entrypoints:\n"
            "- name: crawler\n"
            "  marketplace_card: true\n"
        )

        assert read_entrypoint_names(tmp_path) == ["crawler"]

    def test_all_cardless_is_a_skip_not_a_failure(self, tmp_path: Path) -> None:
        _write_bundle(tmp_path)
        (tmp_path / "atlan.yaml").write_text(
            "name: behind-the-scenes\n"
            "entrypoints:\n"
            "- name: crawler\n"
            "  marketplace_card: false\n"
            "- name: miner\n"
            "  marketplace_card: false\n"
        )

        with pytest.raises(RouteCheckSkipped, match="marketplace_card: false"):
            read_entrypoints(tmp_path)

    def test_a_bundle_declaring_no_entrypoints_still_fails(
        self, tmp_path: Path
    ) -> None:
        """Skip and fail must not be conflated.

        `read_entrypoint_names` returns an empty list both for "all opted out"
        and for "declares none", but only the first is a legitimate skip — the
        second is a nested tree whose cards cannot be located, which is
        incoherent committed state.
        """
        _write_bundle(tmp_path)
        (tmp_path / "atlan.yaml").write_text("name: clickhouse\n")

        with pytest.raises(SetupRouteError, match="declares no entrypoints"):
            read_entrypoints(tmp_path)


class TestCatalogTruncation:
    """A truncated catalog read reports "not installed" — a silent wrong answer.

    It is the worst failure available to a check whose entire value is that it
    does not false-positive, so it must be loud instead.
    """

    class _TruncatingRoutes(_FakeRoutes):
        def __init__(self, apps: list[dict[str, Any]], total: int) -> None:
            super().__init__([apps], _healthy_configmaps())
            self._total = total

        # Re-implements the real `catalog()` guard path rather than the fake's
        # shortcut, so the assertion is against the shipped code.
        def catalog(self) -> list[dict[str, Any]]:
            from application_sdk.testing.setup_routes import TenantRoutes

            self.catalog_reads += 1
            body = {"total": self._total, "apps": self._catalogs[0]}
            return TenantRoutes.catalog(_StubGet(body))  # type: ignore[arg-type]

    def test_a_short_page_fails_loudly(self, tmp_path: Path) -> None:
        _write_bundle(tmp_path)
        routes = self._TruncatingRoutes(
            [_card_payload("clickhouse-crawler", entrypoint="crawler")], total=137
        )

        with pytest.raises(SetupRouteError, match="paginated"):
            verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

    def test_a_complete_page_is_accepted(self, tmp_path: Path) -> None:
        """`total` equal to the page length is the normal, unpaginated case."""
        _write_bundle(tmp_path)
        both = _both_cards()
        routes = self._TruncatingRoutes(both, total=len(both))

        report = verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

        assert len(report) == 2


class _StubGet:
    """Feeds one canned body to the real ``TenantRoutes.catalog`` guard.

    Constructed rather than instantiating ``TenantRoutes`` so no URL validation
    or network setup is involved; ``catalog()`` only calls ``self.get``.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def get(self, path: str) -> tuple[int, object]:
        return 200, self._body


# ---------------------------------------------------------------------------
# Review findings — three shapes that each reported a healthy app as broken
# ---------------------------------------------------------------------------


class TestCardNameCasing:
    """`read_app_name` lowercases atlan.yaml; a card's name is the catalog's.

    Comparing them verbatim reports a mixed-case card as "not installed on this
    tenant" — a false negative wearing the most misleading message this check
    can emit, since it points the reader at the deploy rather than at the match.
    """

    def test_a_mixed_case_card_still_matches(self) -> None:
        catalog = [_card_payload("clickhouse-crawler", name="ClickHouse")]

        cards, reason = locate_cards("clickhouse", _eps("crawler"), catalog)

        assert reason is None
        assert cards["crawler"].id == "clickhouse-crawler"

    def test_a_padded_card_name_still_matches(self) -> None:
        """Neither side is a value whose formatting we control."""
        catalog = [_card_payload("clickhouse-crawler", name="  clickhouse  ")]

        cards, reason = locate_cards("clickhouse", _eps("crawler"), catalog)

        assert reason is None
        assert cards["crawler"].id == "clickhouse-crawler"

    def test_a_genuinely_different_app_still_does_not_match(self) -> None:
        """Case-folding must not widen the match to a different app."""
        catalog = [_card_payload("clickhouse-crawler", name="clickhouse-legacy")]

        cards, reason = locate_cards("clickhouse", _eps("crawler"), catalog)

        assert cards == {}
        assert reason is not None


class TestSoleContractCardMatching:
    """An app's single card, whatever `entrypoint` the catalog gave it.

    A route/card-split app (BLDX-1342) has several `@entrypoint`s behind ONE
    card and therefore one flat generated tree. That card is served carrying a
    real entrypoint name — and an earlier revision keyed such an app under `""`
    and matched on that key, so a perfectly healthy single-card app reported as
    having no card at all.

    Two things fix it, and neither is a guess. Entrypoints never carry an empty
    name: a sole contract is labelled by its committed config id. And a sole
    contract's card is matched by COUNT — one form, one card, nothing to choose
    between — so the card's own `entrypoint` takes no part in the match.

    That is also why no default entrypoint is calculated: every case that might
    have needed one is answered by a fact instead. `@entrypoint(default=True)`
    is a runtime `AppRegistry` flag absent from both `atlan.yaml` and
    `manifest.json`, so a default computed here could only ever be a
    presumption.
    """

    def test_a_sole_card_with_a_named_entrypoint_matches(self, tmp_path: Path) -> None:
        catalog = [_card_payload("mysql", name="mysql", entrypoint="crawler")]

        cards, reason = locate_cards("mysql", _sole("mysql"), catalog)

        assert reason is None
        assert cards["mysql"].id == "mysql"

    def test_a_sole_card_with_no_entrypoint_still_matches(self) -> None:
        """The case that already worked must keep working."""
        catalog = [_card_payload("mysql", name="mysql", entrypoint="")]

        cards, reason = locate_cards("mysql", _sole("mysql"), catalog)

        assert reason is None
        assert cards["mysql"].id == "mysql"

    def test_a_sole_contract_verifies_end_to_end_against_a_named_card(
        self, tmp_path: Path
    ) -> None:
        """The whole check, on the shape that used to report "no card"."""
        _write_flat(tmp_path)
        schema = {
            "properties": {
                "extraction-method": {},
                "credential-guid": {},
                "connection": {},
            }
        }
        routes = _FakeRoutes(
            [[_card_payload("mysql", name="mysql", entrypoint="crawler")]],
            {"mysql": (200, _configmap_response(schema, "mysql"))},
        )

        report = verify(tmp_path, routes, wait_seconds=0)  # type: ignore[arg-type]

        assert len(report) == 1

    def test_two_cards_for_a_sole_contract_is_reported_not_guessed(self) -> None:
        """One form, two cards: the two disagree about the app's shape.

        Picking either would be a coin toss that reports as a pass or as a
        contract break depending on which way it landed, so this names the
        disagreement instead.
        """
        catalog = [
            _card_payload("mysql", name="mysql", entrypoint="crawler"),
            _card_payload("mysql-miner", name="mysql", entrypoint="miner"),
        ]

        cards, reason = locate_cards("mysql", _sole("mysql"), catalog)

        assert cards == {}
        assert reason is not None
        assert "ambiguous" in reason
        assert "crawler" in reason and "miner" in reason

    def test_a_bundle_still_matches_on_entrypoint(self) -> None:
        """The count-match is scoped to a sole contract only.

        A bundle must keep matching card to entrypoint by name, or one entry
        point's form could be checked against another's card.
        """
        catalog = [
            _card_payload("clickhouse-crawler", entrypoint="crawler"),
            _card_payload("clickhouse-miner", entrypoint="miner"),
        ]

        cards, reason = locate_cards("clickhouse", _eps("crawler", "miner"), catalog)

        assert reason is None
        assert cards["crawler"].id == "clickhouse-crawler"
        assert cards["miner"].id == "clickhouse-miner"


class TestRedirectsAreDeclined:
    """`urlopen` follows redirects by default, and that default is dangerous.

    A tenant that bounces an unauthenticated request to a login page would hand
    this check a 200 carrying HTML. A check built on "200 means the form is
    served" cannot tell that from success, and the negative control — the one
    assertion that certifies all the others — is exactly what it disarms.
    """

    @staticmethod
    def _serve(handler_status: int, location: str = "https://login.invalid/sso"):
        """Run a one-request HTTP server returning *handler_status*."""
        import http.server
        import threading

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's name
                self.send_response(handler_status)
                if 300 <= handler_status < 400:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_a_302_stays_a_302(self) -> None:
        """The finding, pinned: following it would report 200 for a login page."""
        from application_sdk.testing.setup_routes import TenantRoutes

        server = self._serve(302)
        try:
            routes = TenantRoutes.__new__(TenantRoutes)
            routes.base_url = f"http://127.0.0.1:{server.server_port}"
            routes.bearer = "unused"

            status, _ = routes.get("/anything")
        finally:
            server.shutdown()

        assert status == 302, (
            "the redirect was followed; a 302 to a login page would arrive as a "
            "200 and the negative control would certify nothing"
        )

    def test_a_200_is_still_a_200(self) -> None:
        """Declining redirects must not disturb the ordinary path."""
        from application_sdk.testing.setup_routes import TenantRoutes

        server = self._serve(200)
        try:
            routes = TenantRoutes.__new__(TenantRoutes)
            routes.base_url = f"http://127.0.0.1:{server.server_port}"
            routes.bearer = "unused"

            status, body = routes.get("/anything")
        finally:
            server.shutdown()

        assert status == 200
        assert body == {"ok": True}

    def test_a_redirect_is_not_a_valid_rejection(self) -> None:
        """The negative control must not accept a 3xx as "correctly rejected".

        A tenant redirecting every request would otherwise look like a tenant
        that discriminates unknown names, which is the vacuous state the
        control exists to detect.
        """
        from application_sdk.testing.setup_routes import _REJECTION_STATUSES

        for redirect in (301, 302, 303, 307, 308):
            assert redirect not in _REJECTION_STATUSES


class TestUnlabelledBundleCard:
    """A bundle's card carrying no entrypoint is reported, never attributed.

    This is the case a calculated default was considered for and rejected. An
    unlabelled card cannot be assigned to a named entrypoint without presuming
    one, and presuming wrong produces "the card points at the wrong form" —
    which reads exactly like a real contract break, sending the reader after a
    regression that does not exist.

    Reporting it is also the more useful answer: the usual cause is an installed
    build that predates the entrypoint split, serving one card where this branch
    generates several. That is a genuine finding, and naming it beats silently
    checking one arbitrary entrypoint against it.
    """

    def test_an_unlabelled_card_is_reported(self) -> None:
        catalog = [_card_payload("clickhouse", name="clickhouse", entrypoint="")]

        cards, reason = locate_cards("clickhouse", _eps("crawler", "miner"), catalog)

        assert reason is not None
        assert "cannot be attributed" in reason
        assert "predates the entrypoint split" in reason
        # It must not have been silently assigned to either entrypoint.
        assert cards == {}

    def test_no_default_is_calculated_anywhere(self) -> None:
        """No alphabetical-first fallback survived the redesign.

        A default computed from committed artifacts is wrong for any app that
        marks a non-alphabetical `@entrypoint(default=True)` — invisible from
        `atlan.yaml` and `manifest.json` alike — so the module must contain no
        such computation to drift back into use.
        """
        import application_sdk.testing.setup_routes as module

        source = Path(module.__file__).read_text(encoding="utf-8")

        assert "calculated_default" not in source
        assert "sorted(names)[0]" not in source
