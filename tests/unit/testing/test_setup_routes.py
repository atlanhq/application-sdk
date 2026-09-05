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
:func:`~application_sdk.testing.setup_routes.served_form` really inverts the
envelope the SDK's own configmap endpoint builds. It drives the live FastAPI
route rather than a hand-written dict, so the two sides of the join are pinned
against each other in-repo — the committed generated file and the response a
tenant proxies. The nesting differs by exactly one level between them
(``handler.service`` serves ``raw.get("config", raw)``), which is the easiest
thing in this whole check to get quietly wrong.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing import setup_routes
from application_sdk.testing.setup_routes import (
    AppIdentity,
    Card,
    Entrypoint,
    FormStep,
    RouteCheckSkipped,
    ServedForm,
    SetupRouteError,
    TenantRoutes,
    declared_inputs,
    foreign_form,
    form_shortfall,
    locate_cards,
    read_app_identity,
    read_entrypoint_names,
    read_entrypoints,
    route_mismatch,
    served_form,
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
    real one. ``anyOf`` is here precisely so a test proves it is NOT read as an
    input set.

    ``steps`` is different, and the difference is load-bearing: it *is* read,
    because the UI draws the wizard from it and a field no step names never
    reaches a user (FND-1680). So the panels here name their fields the way
    every canonical connector's do — verified against ``atlan-metabase-app``,
    ``atlan-openapi-app`` and ``atlan-mysql-app``, where the set of step-named
    properties equals the set of declared properties exactly, in both
    directions, with no exceptions.

    An earlier revision stubbed this as ``[{"id": "credentials"}]``, naming no
    fields at all. That was harmless while only ``properties`` was read and
    became a fixture asserting the opposite of reality the moment rendering
    was checked.
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
            "steps": [
                {
                    "title": "Credential",
                    "description": "Credential Details",
                    "id": "credential",
                    "properties": ["extraction-method", "credential-guid"],
                },
                {
                    "title": "Connection",
                    "description": "Connection Details",
                    "id": "connection",
                    "properties": ["connection"],
                },
            ],
            "anyOf": [{"properties": {}}],
        },
    }


def _id(name: str, display_name: str = "") -> AppIdentity:
    """The committed identity `read_app_identity` builds from `atlan.yaml`."""
    return AppIdentity(name=name, display_name=display_name)


def _ep(name: str, config_id: str, *, is_sole: bool = False) -> Entrypoint:
    """One entrypoint, for the pure checks that take it whole."""
    return Entrypoint(name=name, config_id=config_id, is_sole=is_sole)


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


def _form_schema(*names: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    """A served form carrying *names*, laid out across real wizard steps.

    Steps are not decoration in this fixture and must not be dropped from it.
    The UI draws the form from ``steps``, so a fixture with ``properties``
    alone is a payload that renders blank — and a suite built on those cannot
    tell a rendered form from an invisible one, which is exactly the gap
    FND-1680's metabase page fell through.

    The layout mirrors the shape every canonical connector commits: a
    ``credential`` panel and a ``connection`` panel, each naming the fields it
    puts on screen.

    Args:
        names: Fields that are both defined and drawn.
        extra: Fields defined by the platform and named by no step, so the
            subset tolerance is exercised without pretending they render.
    """
    first, rest = names[:2], names[2:]
    steps = [{"title": "Credential", "id": "credential", "properties": list(first)}]
    if rest:
        steps.append(
            {"title": "Connection", "id": "connection", "properties": list(rest)}
        )
    return {
        "properties": {name: {} for name in (*names, *extra)},
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# route_mismatch — the load-bearing check
# ---------------------------------------------------------------------------


class TestRouteMismatch:
    def test_passes_on_the_shipped_card_shape(self) -> None:
        """The real card and the real generated config agree, so nothing fires."""
        card = Card.from_payload(_card_payload("clickhouse-crawler"))
        assert route_mismatch(_ep("crawler", "clickhouse-crawler"), card) is None

    def test_bites_on_the_packageid_regression(self) -> None:
        """The FND-1593 shape must be reported, or the check is decoration.

        ``packageId = "@atlan/clickhouse"`` moved the card id to
        ``atlan-clickhouse`` while the workflow config stayed
        ``clickhouse-crawler``, and both setup pages 404'd. This is that exact
        divergence.
        """
        card = Card.from_payload(_card_payload("atlan-clickhouse"))

        reason = route_mismatch(_ep("crawler", "clickhouse-crawler"), card)

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

        reason = route_mismatch(_ep("miner", "clickhouse-miner"), card)

        assert reason is not None
        assert "atlan-clickhouse-miner" in reason
        assert "clickhouse-miner" in reason

    def test_names_the_cause_so_the_reader_can_act(self) -> None:
        """The message must point at packageId, the known cause (FND-1659)."""
        card = Card.from_payload(_card_payload("atlan-clickhouse"))

        reason = route_mismatch(_ep("crawler", "clickhouse-crawler"), card)

        assert reason is not None
        assert "packageId" in reason

    def test_reports_a_card_with_no_id(self) -> None:
        """No id at all means the marketplace cannot build a setup link."""
        payload = _card_payload("clickhouse-crawler")
        del payload["id"]

        reason = route_mismatch(
            _ep("crawler", "clickhouse-crawler"), Card.from_payload(payload)
        )

        assert reason is not None
        assert "no id" in reason

    def test_reports_an_ungenerated_workflow_config(self) -> None:
        """An id-less config means the artifacts were never generated."""
        card = Card.from_payload(_card_payload("clickhouse-crawler"))

        reason = route_mismatch(_ep("crawler", ""), card)

        assert reason is not None
        assert "Regenerate the contract" in reason

    def test_a_sole_entrypoint_is_named_by_its_config_id(self) -> None:
        """Never an empty quote in prose, because the name is never empty.

        An earlier revision rendered a flat app as `''` and papered over it with
        a `<flat>` placeholder at every message site. The label is now a
        committed fact, so there is nothing to paper over.

        Driven through the id-less card rather than an id mismatch, because a
        sole contract no longer fails on a mismatch — see
        :class:`TestAppIdFallbackTolerance`.
        """
        payload = _card_payload("atlan-mysql", entrypoint="")
        del payload["id"]

        reason = route_mismatch(
            _ep("mysql", "mysql", is_sole=True), Card.from_payload(payload)
        )

        assert reason is not None
        assert "'mysql'" in reason
        assert "<flat>" not in reason


class TestAppIdFallbackTolerance:
    """FND-1680: a card id differing from the config id is not itself a break.

    The fleet's first live run failed openapi, metabase and mysql on exactly
    this — every one serves a card id of ``atlan-<name>`` against a generated
    config id of ``<name>``, and every one of those setup pages renders. The
    reason is in this repo: ``handler.service.get_configmap`` resolves an
    unmatched id through a documented default-entrypoint fallback, added
    because ``atlan-openapi`` really did 404 in production
    (``test_configmap_default_fallback_serves_flat_single_entrypoint_form``).

    The tolerance is gated on ``is_sole`` because that is where the fallback is
    safe by construction: it resolves to the app's *default* entry point, which
    for a sole contract is the only one. A bundle gets no tolerance — there the
    same fallback would hand every card the default entry point's form.
    """

    def test_a_sole_contract_tolerates_the_app_id_card(self) -> None:
        """The exact openapi / metabase / mysql shape, which is healthy."""
        card = Card.from_payload(_card_payload("atlan-openapi", entrypoint=""))

        assert route_mismatch(_ep("openapi", "openapi", is_sole=True), card) is None

    def test_a_bundle_still_bites_on_the_same_shape(self) -> None:
        """Same divergence, bundle layout: still a real, reportable break."""
        card = Card.from_payload(_card_payload("atlan-clickhouse"))

        reason = route_mismatch(_ep("crawler", "clickhouse-crawler"), card)

        assert reason is not None
        assert "atlan-clickhouse" in reason
        assert "clickhouse-crawler" in reason
        # It must say WHY a bundle is different, or the next reader "fixes" the
        # asymmetry by deleting it.
        assert "DEFAULT entry point" in reason

    def test_the_tolerance_is_not_a_free_pass(self, tmp_path: Path) -> None:
        """A tolerated id still has to actually serve this contract's form.

        Equality was only ever a proxy for "the card's id resolves to this
        entry point's form". Dropping it for a sole contract must not drop the
        assertion — `verify` still fetches the card id and still requires the
        served schema to carry every declared input.
        """
        _write_flat(tmp_path)
        catalog = [_card_payload("atlan-mysql", name="mysql", entrypoint="")]
        # Served under the card id, but one input short of the contract.
        stale = _form_schema("extraction-method", "credential-guid")
        routes = _FakeRoutes(
            [catalog],
            {"atlan-mysql": (200, _configmap_response(stale, "atlan-mysql"))},
        )

        with pytest.raises(SetupRouteError, match="connection"):
            verify(tmp_path, routes, wait_seconds=0)

    def test_a_tolerated_id_is_reported_not_silently_passed(
        self, tmp_path: Path
    ) -> None:
        """A green run must still say the fallback answered, not just "resolves".

        A tolerance nobody can see in the log is indistinguishable from a check
        that stopped looking.
        """
        _write_flat(tmp_path)
        catalog = [_card_payload("atlan-mysql", name="mysql", entrypoint="")]
        served = _form_schema("extraction-method", "credential-guid", "connection")
        routes = _FakeRoutes(
            [catalog],
            {"atlan-mysql": (200, _configmap_response(served, "atlan-mysql"))},
        )

        report = verify(tmp_path, routes, wait_seconds=0)

        assert len(report) == 1
        assert "/workflows/setup/atlan-mysql resolves" in report[0]
        assert "app-id fallback" in report[0]


# ---------------------------------------------------------------------------
# form_shortfall — does the contract's field actually reach a user
# ---------------------------------------------------------------------------


def _served(
    properties: set[str] | None = None,
    steps: dict[str, list[str]] | None = None,
) -> ServedForm:
    """A parsed served form, from field names and step-id -> field-names."""
    return ServedForm(
        properties=frozenset(properties or set()),
        steps=tuple(
            FormStep(id=step_id, properties=frozenset(names))
            for step_id, names in (steps or {}).items()
        ),
    )


def _declaring(*names: str) -> Entrypoint:
    """A sole entrypoint declaring exactly *names*."""
    return Entrypoint(
        name="crawler",
        config_id="clickhouse-crawler",
        is_sole=True,
        declared=frozenset(names),
    )


class TestFormShortfall:
    def test_extra_served_fields_are_not_a_failure(self) -> None:
        """A subset check: the platform may decorate the schema.

        Failing on platform-added fields buys no safety and would red the fleet
        the first time the platform grew one.
        """
        served = _served(
            {"credential-guid", "connection", "labFlag", "platform-injected"},
            {"credential": ["credential-guid"], "connection": ["connection"]},
        )

        assert (
            form_shortfall(_declaring("credential-guid", "connection"), served) is None
        )

    def test_bites_on_a_missing_declared_input(self) -> None:
        """A missing input is the signal — a stale image, or a change that never landed."""
        served = _served(
            {"credential-guid", "connection"},
            {"credential": ["credential-guid"], "connection": ["connection"]},
        )

        reason = form_shortfall(
            _declaring("credential-guid", "connection", "include-filter"), served
        )

        assert reason is not None
        assert "include-filter" in reason
        # The served set has to be in the message, or the reader cannot tell a
        # stale image from a renamed field.
        assert "credential-guid" in reason

    def test_a_contract_declaring_nothing_is_reported(self) -> None:
        """Zero declared inputs makes the check vacuous, so it must not pass."""
        reason = form_shortfall(_declaring(), _served({"anything"}))

        assert reason is not None
        assert "declares no inputs" in reason


class TestForeignForm:
    """FND-1680: `metadata.name` is a rewrite, not an identity.

    An earlier revision asserted `served_name == card.id` and failed all three
    mysql legs on a healthy app: it asked for `atlan-mysql` and the response
    carried `metadata.name: 'mysql'`. Both are right. Heracles resolves the
    marketplace app id to the configmap name *before* proxying, so the pod
    never saw `atlan-mysql` — while metabase's response carried
    `metadata.name: 'atlan-metabase'`, because there the app id reached the pod
    unrewritten and hit its default-entrypoint fallback.

    Two real payloads, two different rules, one field. So this speaks up only
    about a name it can attribute to one of THIS repo's committed entry points,
    and says nothing about a value produced by somebody else's rewrite.
    """

    @staticmethod
    def _bundle() -> list[Entrypoint]:
        return [
            _ep("crawler", "clickhouse-crawler"),
            _ep("miner", "clickhouse-miner"),
        ]

    def test_another_entrypoints_form_is_reported(self) -> None:
        """The failure the old assertion was actually reaching for."""
        siblings = self._bundle()

        reason = foreign_form(siblings[0], siblings, "clickhouse-miner")

        assert reason is not None
        assert "another entry point's form" in reason
        assert "'miner'" in reason

    def test_the_heracles_rewrite_is_not_a_failure(self) -> None:
        """The exact mysql shape: asked `atlan-mysql`, served `mysql`."""
        sole = [_ep("mysql", "mysql", is_sole=True)]

        assert foreign_form(sole[0], sole, "mysql") is None

    def test_an_unrewritten_app_id_is_not_a_failure(self) -> None:
        """The exact metabase shape: asked `atlan-metabase`, served the same."""
        sole = [_ep("metabase", "metabase", is_sole=True)]

        assert foreign_form(sole[0], sole, "atlan-metabase") is None

    def test_an_unattributable_name_says_nothing(self) -> None:
        """Silence, not a guess. Content is what covers this case.

        A name belonging to no declared entry point could be a rewrite, an
        alias, or a genuine fault, and this module cannot tell which without
        re-implementing Heracles. `form_shortfall` checks what the payload
        CONTAINS, which is immune to every rename in the chain.
        """
        siblings = self._bundle()

        assert foreign_form(siblings[0], siblings, "something-else") is None
        assert foreign_form(siblings[0], siblings, "") is None
        assert foreign_form(siblings[0], siblings, None) is None

    def test_its_own_config_id_is_never_foreign(self) -> None:
        siblings = self._bundle()

        assert foreign_form(siblings[0], siblings, "clickhouse-crawler") is None


class TestBlankFormDetection:
    """FND-1680: a 200 does not mean a user sees a form.

    The metabase setup page answered 200 for ``atlan-metabase`` and rendered
    blank. Its ``data.config`` was the app's ``artifact_schemas.json`` —
    ``{"version": 1, "schemas": {...}}`` — because the pod ran an SDK without
    the artifact-schemas exclusion and served the declaration file as the form.
    Every status-code assertion in the check passed.

    These pin the four ways a served payload fails to become a rendered form,
    each with its own diagnosis, because "the page is blank" has more than one
    cause and a single message for all of them sends the reader the wrong way.
    """

    def test_the_metabase_payload_is_reported_as_not_a_form(self) -> None:
        """The exact live payload, verbatim from the tenant."""
        body = _configmap_response(
            {
                "version": 1,
                "schemas": {"residual_failures": {"format": "ndjson", "fields": []}},
            },
            "atlan-metabase",
        )

        reason = form_shortfall(
            _declaring("credential-guid", "connection"), served_form(body)
        )

        assert reason is not None
        assert "is not a setup form" in reason
        assert "renders blank" in reason
        # Name the cause, or the reader chases a stale image that is not the problem.
        assert "artifact_schemas.json" in reason

    def test_fields_without_steps_are_reported(self) -> None:
        """A schema with fields and no panels draws nothing at all."""
        served = _served({"credential-guid", "connection"})

        reason = form_shortfall(_declaring("credential-guid", "connection"), served)

        assert reason is not None
        assert "no 'steps'" in reason
        assert "renders blank" in reason

    def test_a_field_no_step_names_is_reported(self) -> None:
        """Served but never drawn — invisible to the user filling the form in.

        This is the gap a properties-only check cannot see: every declared
        input is present in the schema, and one of them still never appears
        on screen.
        """
        served = _served(
            {"credential-guid", "connection", "include-filter"},
            {"credential": ["credential-guid"], "connection": ["connection"]},
        )

        reason = form_shortfall(
            _declaring("credential-guid", "connection", "include-filter"), served
        )

        assert reason is not None
        assert "include-filter" in reason
        assert "named by no step" in reason
        # The panels that DO exist, so the reader can see where it should have been.
        assert "credential" in reason

    def test_a_fully_rendered_form_passes(self) -> None:
        """Every declared field defined AND drawn is the only passing shape."""
        served = _served(
            {"extraction-method", "credential-guid", "connection"},
            {
                "credential": ["extraction-method", "credential-guid"],
                "connection": ["connection"],
            },
        )

        assert (
            form_shortfall(
                _declaring("extraction-method", "credential-guid", "connection"), served
            )
            is None
        )

    def test_the_causes_are_reported_in_cause_order(self) -> None:
        """A payload that is not a form must not be reported as a stale image.

        Both faults are true of the metabase payload — it is not a form, AND
        every declared field is missing from it. Reporting the second sends the
        reader to look for an image bump that would never have fixed it.
        """
        body = _configmap_response({"version": 1, "schemas": {}}, "atlan-metabase")

        reason = form_shortfall(_declaring("connection"), served_form(body))

        assert reason is not None
        assert "is not a setup form" in reason
        assert "older image" not in reason


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

        cards, reason = locate_cards(_id("clickhouse"), _eps("crawler"), catalog)

        assert reason is None
        assert set(cards) == {"crawler"}
        assert cards["crawler"].id == "clickhouse-crawler"

    def test_reports_an_app_absent_from_the_catalog(self) -> None:
        catalog = [_card_payload("mssql-crawler", name="mssql")]

        cards, reason = locate_cards(_id("clickhouse"), _eps("crawler"), catalog)

        assert cards == {}
        assert reason is not None
        assert "not installed on this tenant" in reason
        # The catalog size proves the read worked, which distinguishes "not
        # installed" from "token cannot read the catalog".
        assert "1 apps" in reason
        # And the names it DID carry, so a reader can confirm the absence from
        # the failure instead of from a follow-up investigation.
        assert "'mssql'" in reason

    def test_reports_an_entrypoint_with_no_card(self) -> None:
        """An entrypoint with no card has no setup page at all."""
        catalog = [_card_payload("clickhouse-crawler", name="clickhouse")]

        cards, reason = locate_cards(
            _id("clickhouse"), _eps("crawler", "miner"), catalog
        )

        assert set(cards) == {"crawler"}
        assert reason is not None
        assert "miner" in reason

    def test_a_sole_contract_card_keys_under_its_config_id(self) -> None:
        """No empty-string key anywhere, and the card matches by count.

        The card's own `entrypoint` takes no part in the match — there is one
        card and one form, so there is nothing to choose between.
        """
        catalog = [_card_payload("mysql", name="mysql", entrypoint="")]

        cards, reason = locate_cards(_id("mysql"), _sole("mysql"), catalog)

        assert reason is None
        assert cards["mysql"].id == "mysql"


class TestAppIdentity:
    """FND-1680: the catalog is not consistent about which name a card carries.

    One live tenant, one run: openapi and metabase were listed under their wire
    ``name``, mysql under its ``display_name`` (``MySQL Assets``). A locator
    keyed on either field alone reports a healthy, installed app as absent —
    the most misleading message this check can emit — so it accepts both.
    """

    def test_locates_the_card_the_live_run_could_not(self, tmp_path: Path) -> None:
        """The exact mysql shape the fleet's first run failed on."""
        (tmp_path / "atlan.yaml").write_text(
            "name: mysql\ndisplay_name: MySQL Assets\n"
        )
        identity = read_app_identity(tmp_path)
        catalog = [_card_payload("atlan-mysql", name="MySQL Assets", entrypoint="")]

        cards, reason = locate_cards(identity, _sole("mysql"), catalog)

        assert reason is None
        assert cards["mysql"].id == "atlan-mysql"

    def test_the_wire_name_still_locates_it(self) -> None:
        """openapi and metabase matched on `name`, and must keep doing so."""
        catalog = [_card_payload("atlan-openapi", name="openapi", entrypoint="")]

        cards, reason = locate_cards(
            _id("openapi", "openapi spec loader"), _sole("openapi"), catalog
        )

        assert reason is None
        assert cards["openapi"].id == "atlan-openapi"

    def test_an_undeclared_display_name_yields_one_label(self, tmp_path: Path) -> None:
        (tmp_path / "atlan.yaml").write_text("name: openapi\n")

        assert read_app_identity(tmp_path).labels == ("openapi",)

    def test_a_display_name_equal_to_the_name_is_not_doubled(self) -> None:
        """Deduplicated, so a message never reads `['mysql', 'mysql']`."""
        assert _id("mysql", "mysql").labels == ("mysql",)

    def test_the_display_name_is_case_folded_too(self, tmp_path: Path) -> None:
        """Both sides normalised, or `MySQL Assets` never matches itself."""
        (tmp_path / "atlan.yaml").write_text(
            "name: mysql\ndisplay_name: MySQL Assets\n"
        )
        identity = read_app_identity(tmp_path)

        assert identity.labels == ("mysql", "mysql assets")
        assert identity.matches(Card(id="x", name="  MySQL Assets  "))

    def test_a_genuinely_different_app_is_still_not_matched(self) -> None:
        """Widening the locator must not make it match neighbours."""
        identity = _id("mysql", "mysql assets")

        assert not identity.matches(Card(id="x", name="mssql"))
        assert not identity.matches(Card(id="x", name="mysql assets extra"))


class TestCatalogEvidence:
    """FND-1680: a card lookup that finds nothing has to say what it DID find.

    The first fleet-wide run reported ``no marketplace card has name='mysql'.
    The tenant listed 139 apps`` and then waited out the full reconcile poll —
    on a tenant the preceding step had already proven was running the app at
    the version under test. Two very different faults produce that line: the
    app really is absent, or ``name`` is not the field carrying it. The message
    named neither a single card nor a single field, so the log could not
    distinguish them and the next step was a manual catalog read.

    Nothing here changes what is matched. Deciding to match a different field
    needs the evidence these tests make the failure carry, and inventing a
    fallback field before seeing it would be the soften-the-check move this
    issue explicitly rules out.
    """

    def test_names_the_field_that_actually_carries_the_app_name(self) -> None:
        """A card matching on `id` says so, and contradicts "not installed"."""
        catalog = [
            _card_payload("mysql", name="MySQL Assets", entrypoint="crawler"),
            _card_payload("mssql-crawler", name="mssql"),
        ]

        _, reason = locate_cards(_id("mysql"), _sole("mysql"), catalog)

        assert reason is not None
        assert "reading the wrong field" in reason
        assert "id='mysql'" in reason
        assert "name='MySQL Assets'" in reason
        assert "entrypoint='crawler'" in reason
        # The wrong conclusion must NOT also be offered alongside the right one.
        assert "not installed on this tenant" not in reason

    def test_an_entrypoint_field_match_is_reported_too(self) -> None:
        catalog = [
            _card_payload("atlan-mysql", name="MySQL Assets", entrypoint="mysql")
        ]

        _, reason = locate_cards(_id("mysql"), _sole("mysql"), catalog)

        assert reason is not None
        assert "reading the wrong field" in reason
        assert "entrypoint='mysql'" in reason

    def test_a_decorated_identity_is_shown_rather_than_hidden_in_a_slice(
        self,
    ) -> None:
        """A near-miss card beats an alphabetical sample that may not reach it.

        The catalog runs to ~140 cards, so a bounded sorted sample has roughly
        a one-in-seven chance of containing the card that explains the miss.
        A substring hit is reported directly instead.
        """
        catalog = [
            _card_payload(f"app-{index:03d}-crawler", name=f"app-{index:03d}")
            for index in range(139)
        ]
        catalog.append(
            _card_payload(
                "atlan-mysql-crawler", name="Atlan MySQL", entrypoint="crawler"
            )
        )

        _, reason = locate_cards(_id("mysql"), _sole("mysql"), catalog)

        assert reason is not None
        assert "as a substring" in reason
        assert "id='atlan-mysql-crawler'" in reason
        assert "name='Atlan MySQL'" in reason
        # The weaker conclusions must not be offered alongside it.
        assert "not installed on this tenant" not in reason
        assert "reading the wrong field" not in reason

    def test_a_genuine_absence_still_reads_as_not_installed(self) -> None:
        catalog = [
            _card_payload(f"app-{index}-crawler", name=f"app-{index}")
            for index in range(3)
        ]

        _, reason = locate_cards(_id("mysql"), _sole("mysql"), catalog)

        assert reason is not None
        assert "not installed on this tenant" in reason
        assert "'app-0'" in reason

    def test_the_name_sample_is_bounded(self) -> None:
        """~140 cards is the live scale; the whole list would bury the finding.

        Bounded and *sorted*, so the sample is the same slice on every run
        rather than whatever order the catalog happened to serve.
        """
        catalog = [
            _card_payload(f"app-{index:03d}-crawler", name=f"app-{index:03d}")
            for index in range(139)
        ]

        _, reason = locate_cards(_id("mysql"), _sole("mysql"), catalog)

        assert reason is not None
        assert "139 apps" in reason
        assert "20 of 139" in reason
        assert "'app-000'" in reason
        assert "'app-019'" in reason
        assert "'app-020'" not in reason

    def test_the_evidence_survives_the_catalog_poll(self, tmp_path: Path) -> None:
        """`_await_cards` re-reads the catalog, and the sample must not vanish.

        The lookup consumed the payload list twice — once to filter, once to
        describe — so a streamed generator would leave the diagnostic with an
        exhausted iterator and an empty sample on exactly the failing path.
        """
        _write_flat(tmp_path)
        routes = _FakeRoutes([[_card_payload("mysql", name="MySQL Assets")]], {})

        with pytest.raises(SetupRouteError) as excinfo:
            verify(tmp_path, routes, wait_seconds=0)

        message = str(excinfo.value)
        assert "id='mysql'" in message
        assert "reading the wrong field" in message
        assert "Waited 0s" in message


# ---------------------------------------------------------------------------
# served_form — the one-level nesting difference, and both rendered parts
# ---------------------------------------------------------------------------


class TestServedForm:
    def test_unwraps_the_json_string_config(self) -> None:
        response = _configmap_response(
            {"properties": {"credential-guid": {}, "connection": {}}}
        )

        assert served_form(response).properties == frozenset(
            {"credential-guid", "connection"}
        )

    def test_reads_the_steps_the_wizard_draws_from(self) -> None:
        """The real mysql shape: step ids and the fields each panel carries."""
        response = _configmap_response(
            {
                "properties": {
                    "extraction-method": {},
                    "credential-guid": {},
                    "connection": {},
                },
                "steps": [
                    {
                        "title": "Credential",
                        "id": "credential",
                        "properties": ["extraction-method", "credential-guid"],
                    },
                    {
                        "title": "Connection",
                        "id": "connection",
                        "properties": ["connection"],
                    },
                ],
            }
        )

        form = served_form(response)

        assert [step.id for step in form.steps] == ["credential", "connection"]
        assert form.rendered == frozenset(
            {"extraction-method", "credential-guid", "connection"}
        )

    def test_rendered_is_the_union_over_steps_not_over_properties(self) -> None:
        """The whole point: a defined-but-undrawn field is not rendered."""
        response = _configmap_response(
            {
                "properties": {"connection": {}, "orphan": {}},
                "steps": [{"id": "connection", "properties": ["connection"]}],
            }
        )

        form = served_form(response)

        assert form.properties == frozenset({"connection", "orphan"})
        assert form.rendered == frozenset({"connection"})

    def test_a_form_with_no_steps_renders_nothing(self) -> None:
        """`rendered` must be empty, not raise, when there are no panels."""
        form = served_form(_configmap_response({"properties": {"connection": {}}}))

        assert form.steps == ()
        assert form.rendered == frozenset()

    def test_malformed_steps_are_skipped_not_fatal(self) -> None:
        """A junk entry is dropped; the good panels still report.

        Degrading here rather than raising is what lets `form_shortfall` report
        the shortfall WITH the evidence instead of the parser crashing on it.
        """
        response = _configmap_response(
            {
                "properties": {"connection": {}},
                "steps": [
                    "not-a-step",
                    {"id": "connection", "properties": ["connection"]},
                    {"id": "broken", "properties": "not-a-list"},
                ],
            }
        )

        form = served_form(response)

        assert [step.id for step in form.steps] == ["connection", "broken"]
        assert form.rendered == frozenset({"connection"})

    def test_a_step_with_no_id_gets_a_positional_label(self) -> None:
        """Messages name the panel, so an id-less step still needs a label."""
        response = _configmap_response(
            {"properties": {"a": {}}, "steps": [{"properties": ["a"]}]}
        )

        assert served_form(response).steps[0].id == "<step 0>"

    def test_rejects_a_non_string_config(self) -> None:
        """A nested object instead of a string means the shape changed.

        Silently coping would make the subset check read an empty served set
        and report every declared input as missing.
        """
        response = _configmap_response({"properties": {}})
        response["data"] = {"config": {"properties": {}}}

        with pytest.raises(SetupRouteError, match="no string data.config"):
            served_form(response)

    def test_rejects_a_response_with_no_data(self) -> None:
        with pytest.raises(SetupRouteError, match="no 'data' object"):
            served_form({"metadata": {"name": "x"}})

    def test_rejects_unparseable_config(self) -> None:
        response = _configmap_response({"properties": {}})
        response["data"] = {"config": "{not json"}

        with pytest.raises(SetupRouteError, match="not valid JSON"):
            served_form(response)

    def test_a_schema_with_no_properties_serves_nothing(self) -> None:
        """Empty, not an error — ``form_shortfall`` is what reports the gap."""
        assert served_form(_configmap_response({"steps": []})).properties == frozenset()


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
        # A bundle emits one per entrypoint. It sorts FIRST of the three —
        # see `_write_flat` and TestNonFormSiblings for why that matters.
        (directory / "artifact_schemas.json").write_text(
            json.dumps({"version": 1, "artifacts": {}})
        )
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
    # The full sibling set a generated tree really carries, in the order it
    # sorts: `artifact_schemas` first, then the credential template, then the
    # manifest, then the form. Both of the first two sort BEFORE `mysql.json`,
    # which is the whole point — see TestNonFormSiblings.
    (generated / "artifact_schemas.json").write_text(
        json.dumps({"version": 1, "artifacts": {}})
    )
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

    def test_a_flat_app_does_not_pick_a_non_form_sibling(self, tmp_path: Path) -> None:
        """Exclusion by stem, not by hoping the sibling lacks a key.

        Both ``artifact_schemas.json`` and ``atlan-connectors-mysql.json``
        sort alphabetically BEFORE ``mysql.json``, so file selection in a flat
        tree has to reject them explicitly rather than relying on ordering.

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

    def test_a_flat_app_with_artifact_schemas_reads_its_real_config_id(
        self, tmp_path: Path
    ) -> None:
        """FND-1680, from this module's side of the join.

        ``_generated_tree`` owns the rule that ``artifact_schemas`` is not a
        form, and ``tests/unit/app/test_generated_tree.py`` pins it there. What
        this pins is that ``read_entrypoints`` inherits it: the openapi leg
        died on ``artifact_schemas.json carries no top-level 'id'`` — a
        SetupRouteError about the wrong file, raised right here — so the check
        reading the same authority as the server is the thing under test, not
        the rule itself.
        """
        _write_flat(tmp_path)

        found = read_entrypoints(tmp_path)

        assert [(e.name, e.config_id) for e in found] == [("mysql", "mysql")]
        assert found[0].source is not None
        assert found[0].source.name == "mysql.json"

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

        assert read_app_identity(tmp_path).name == "clickhouse"

    def test_a_missing_atlan_yaml_is_a_clear_error(self, tmp_path: Path) -> None:
        with pytest.raises(SetupRouteError, match="cannot read"):
            read_app_identity(tmp_path)

    def test_a_nameless_atlan_yaml_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "atlan.yaml").write_text("app_id: x\n")

        with pytest.raises(SetupRouteError, match='no top-level "name"'):
            read_app_identity(tmp_path)

    def test_entrypoint_names_are_empty_for_a_flat_app(self, tmp_path: Path) -> None:
        _write_flat(tmp_path)

        assert read_entrypoint_names(tmp_path) == []


# ---------------------------------------------------------------------------
# The round trip — the two sides of the join, pinned against each other
# ---------------------------------------------------------------------------


class TestServedFormRoundTrip:
    """Does ``served_form`` really invert what the SDK's endpoint serves?

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

        If ``served_form`` read one nesting level too deep or too shallow,
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
            form_shortfall(
                Entrypoint(
                    name="crawler",
                    config_id="clickhouse-crawler",
                    declared=declared_inputs(committed),
                ),
                served_form(body),
            )
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
        reason = form_shortfall(
            Entrypoint(
                name="crawler", config_id="clickhouse-crawler", declared=declared
            ),
            served_form(body),
        )

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
    schema = _form_schema(
        "extraction-method",
        "credential-guid",
        "connection",
        # A platform-added field the contract never named and no step draws, so
        # the subset tolerance is exercised on the happy path too.
        extra=("labFlag",),
    )
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

        report = verify(tmp_path, routes, wait_seconds=0)

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

        verify(tmp_path, routes, wait_seconds=0)

        assert routes.asked[0] == "clickhouse-nonexistent-setup-route-check"

    def test_fails_when_unknown_names_are_not_rejected(self, tmp_path: Path) -> None:
        """A 200 for a name that cannot exist makes every assertion vacuous."""
        _write_bundle(tmp_path)
        routes = _FakeRoutes([_both_cards()], _healthy_configmaps(), unknown_status=200)

        with pytest.raises(SetupRouteError, match="proves anything"):
            verify(tmp_path, routes, wait_seconds=0)

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
            verify(tmp_path, routes, wait_seconds=0)

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
            verify(tmp_path, routes, wait_seconds=0)

    def test_fails_when_the_endpoint_serves_a_different_name(
        self, tmp_path: Path
    ) -> None:
        """Asked for one form, served another — the page renders the wrong one."""
        _write_bundle(tmp_path)
        configmaps = _healthy_configmaps()
        # The crawler's route serves a response identifying itself as the
        # MINER's form — a name this repo can attribute, which is the only
        # kind `foreign_form` speaks up about.
        schema = _form_schema("extraction-method", "credential-guid", "connection")
        configmaps["clickhouse-crawler"] = (
            200,
            _configmap_response(schema, "clickhouse-miner"),
        )
        routes = _FakeRoutes([_both_cards()], configmaps)

        with pytest.raises(SetupRouteError, match="another entry point's form"):
            verify(tmp_path, routes, wait_seconds=0)

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
            routes,
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
            module.verify(tmp_path, routes, wait_seconds=0)

        message = str(caught.value)
        assert "miner" in message
        assert "Waited 0s" in message

    def test_skips_an_app_with_no_generated_contract(self, tmp_path: Path) -> None:
        """Skip-not-fail, or this is a fleet-wide false positive on day one."""
        (tmp_path / "atlan.yaml").write_text("name: behind-the-scenes\n")
        routes = _FakeRoutes([[]], {})

        with pytest.raises(RouteCheckSkipped):
            verify(tmp_path, routes, wait_seconds=0)

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

        report = verify(tmp_path, routes, wait_seconds=0)

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
            # Calling the real guard as an unbound method with a stub self:
            # `_StubGet` supplies only `get`, which is all `catalog` touches.
            return TenantRoutes.catalog(_StubGet(body))  # type: ignore[arg-type] — stub supplies the only attribute `catalog` reads

    def test_a_short_page_fails_loudly(self, tmp_path: Path) -> None:
        _write_bundle(tmp_path)
        routes = self._TruncatingRoutes(
            [_card_payload("clickhouse-crawler", entrypoint="crawler")], total=137
        )

        with pytest.raises(SetupRouteError, match="paginated"):
            verify(tmp_path, routes, wait_seconds=0)

    def test_a_complete_page_is_accepted(self, tmp_path: Path) -> None:
        """`total` equal to the page length is the normal, unpaginated case."""
        _write_bundle(tmp_path)
        both = _both_cards()
        routes = self._TruncatingRoutes(both, total=len(both))

        report = verify(tmp_path, routes, wait_seconds=0)

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
    """`read_app_identity` lowercases atlan.yaml; a card's name is the catalog's.

    Comparing them verbatim reports a mixed-case card as "not installed on this
    tenant" — a false negative wearing the most misleading message this check
    can emit, since it points the reader at the deploy rather than at the match.
    """

    def test_a_mixed_case_card_still_matches(self) -> None:
        catalog = [_card_payload("clickhouse-crawler", name="ClickHouse")]

        cards, reason = locate_cards(_id("clickhouse"), _eps("crawler"), catalog)

        assert reason is None
        assert cards["crawler"].id == "clickhouse-crawler"

    def test_a_padded_card_name_still_matches(self) -> None:
        """Neither side is a value whose formatting we control."""
        catalog = [_card_payload("clickhouse-crawler", name="  clickhouse  ")]

        cards, reason = locate_cards(_id("clickhouse"), _eps("crawler"), catalog)

        assert reason is None
        assert cards["crawler"].id == "clickhouse-crawler"

    def test_a_genuinely_different_app_still_does_not_match(self) -> None:
        """Case-folding must not widen the match to a different app."""
        catalog = [_card_payload("clickhouse-crawler", name="clickhouse-legacy")]

        cards, reason = locate_cards(_id("clickhouse"), _eps("crawler"), catalog)

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

        cards, reason = locate_cards(_id("mysql"), _sole("mysql"), catalog)

        assert reason is None
        assert cards["mysql"].id == "mysql"

    def test_a_sole_card_with_no_entrypoint_still_matches(self) -> None:
        """The case that already worked must keep working."""
        catalog = [_card_payload("mysql", name="mysql", entrypoint="")]

        cards, reason = locate_cards(_id("mysql"), _sole("mysql"), catalog)

        assert reason is None
        assert cards["mysql"].id == "mysql"

    def test_a_sole_contract_verifies_end_to_end_against_a_named_card(
        self, tmp_path: Path
    ) -> None:
        """The whole check, on the shape that used to report "no card"."""
        _write_flat(tmp_path)
        schema = _form_schema("extraction-method", "credential-guid", "connection")
        routes = _FakeRoutes(
            [[_card_payload("mysql", name="mysql", entrypoint="crawler")]],
            {"mysql": (200, _configmap_response(schema, "mysql"))},
        )

        report = verify(tmp_path, routes, wait_seconds=0)

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

        cards, reason = locate_cards(_id("mysql"), _sole("mysql"), catalog)

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

        cards, reason = locate_cards(
            _id("clickhouse"), _eps("crawler", "miner"), catalog
        )

        assert reason is None
        assert cards["crawler"].id == "clickhouse-crawler"
        assert cards["miner"].id == "clickhouse-miner"


class _FakeClock:
    """A monotonic clock that advances only when the code under test sleeps.

    Installed with ``monkeypatch.setattr(setup_routes, "time", clock)``, which
    swaps the name *inside that module's namespace* rather than mutating the
    stdlib ``time`` module. That distinction matters twice over: patching
    ``time.monotonic`` globally is shared with the asyncio event loop and has
    made this suite flaky before, and patching only ``sleep`` while the
    deadline still reads the real clock turns every bounded wait into a busy
    spin — 62 seconds at 98% CPU for three tests, which is how this helper
    came to exist.

    Advancing on ``sleep`` also makes the elapsed time assertable, so a test
    can pin how long a wait actually lasted instead of only that it happened.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestRolloutLag:
    """FND-1680: the install verdict is not the pod's verdict.

    ``prepare-tenant`` resolves "the tenant runs the version under test" from
    LM's *catalog record*, which flips when the install lands — while the
    HelmRelease rollout that replaces the pod lags it. The aws leg measured the
    gap: ``verified: tenant runs sdr-test-634b735e`` at 12:50:02, and six
    seconds later the pod served ``extraction_method``, the spelling that
    connector had renamed to ``extraction-method`` two days before. Azure ran
    the identical assertions against the identical build and passed, because
    its pod had already rolled.

    So the form gets the same bounded wait the card lookup already had. Both
    halves are pinned here: that a lagging rollout is waited out, and that a
    shortfall which survives the window is still reported — and says it waited,
    so nobody reads a real contract break as flake.
    """

    @staticmethod
    def _sequenced(catalog: list[dict[str, Any]], bodies: list[dict[str, Any]]) -> Any:
        """Routes whose configmap answer changes between reads, as a rollout does."""

        class _Rolling:
            def __init__(self) -> None:
                self.reads = 0

            def catalog(self) -> list[dict[str, Any]]:
                return catalog

            def configmap(self, name: str) -> tuple[int, dict[str, Any]]:
                if name not in {"atlan-mysql", "mysql"}:
                    return 404, {}
                body = bodies[min(self.reads, len(bodies) - 1)]
                self.reads += 1
                return 200, body

        return _Rolling()

    def test_a_lagging_rollout_is_waited_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stale form first, current form second: the leg passes, not flakes."""
        monkeypatch.setattr(setup_routes, "time", _FakeClock())
        _write_flat(tmp_path)
        stale = _configmap_response(
            # The previous image's spelling, exactly as aws served it.
            _form_schema("extraction_method", "credential-guid", "connection"),
            "atlan-mysql",
        )
        current = _configmap_response(
            _form_schema("extraction-method", "credential-guid", "connection"),
            "atlan-mysql",
        )
        routes = self._sequenced(
            [_card_payload("atlan-mysql", name="mysql", entrypoint="")],
            [stale, current],
        )

        report = verify(tmp_path, routes, wait_seconds=60)

        assert len(report) == 1
        assert "resolves" in report[0]
        assert routes.reads == 2

    def test_a_real_shortfall_still_fails_and_says_it_waited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A contract that never reaches the tenant must not become a pass.

        The whole risk of adding a wait is that it converts a finding into
        patience. The message has to rule the lag out explicitly, or the next
        reader discounts a genuine break as one.
        """
        clock = _FakeClock()
        monkeypatch.setattr(setup_routes, "time", clock)
        _write_flat(tmp_path)
        stale = _configmap_response(
            _form_schema("extraction_method", "credential-guid", "connection"),
            "atlan-mysql",
        )
        routes = self._sequenced(
            [_card_payload("atlan-mysql", name="mysql", entrypoint="")], [stale]
        )

        with pytest.raises(SetupRouteError) as excinfo:
            verify(tmp_path, routes, wait_seconds=30)

        message = str(excinfo.value)
        assert "extraction-method" in message
        assert "Still true after 30s" in message
        assert "rollout lagging its catalog record" in message
        # It really polled rather than reporting the first read, and it really
        # spent the budget it says it spent.
        assert routes.reads > 1
        assert sum(clock.slept) >= 30

    def test_progress_names_the_entrypoint_it_is_waiting_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A silent wait is indistinguishable from a hang, per the CLI's own rule."""
        monkeypatch.setattr(setup_routes, "time", _FakeClock())
        _write_flat(tmp_path)
        stale = _configmap_response(_form_schema("nothing-declared"), "atlan-mysql")
        routes = self._sequenced(
            [_card_payload("atlan-mysql", name="mysql", entrypoint="")], [stale]
        )
        notes: list[str] = []

        with pytest.raises(SetupRouteError):
            verify(tmp_path, routes, wait_seconds=30, on_progress=notes.append)

        assert notes
        assert any("setup route not ready yet" in note for note in notes)
        assert all("mysql" in note for note in notes)


class TestTransientRetries:
    """FND-1680: one dropped packet must not decide the leg's verdict.

    The gcp leg died on ``GET /api/service/configmaps/atlan-openapi ... The
    read operation timed out`` while azure passed the identical assertions
    against the identical build minutes apart. That is network weather, not a
    broken setup route, and a check whose verdict tracks the weather gets
    ignored.

    These drive ``TenantRoutes.get`` over a fake opener and count attempts, so
    both halves are pinned: that a transient fault IS retried, and that an
    endpoint's genuine answer is NOT — retrying a 404 would make the negative
    control three times slower for the same verdict, and could dress a real
    rejection up as a flake.
    """

    @staticmethod
    def _routes() -> TenantRoutes:
        return TenantRoutes(base_url="https://tenant.example.invalid", bearer="t")

    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, responses: list[object]) -> list[int]:
        """Answer each successive open from *responses*; count the calls.

        A response entry that is an exception instance is raised; anything else
        is returned as the opened response.
        """
        calls: list[int] = []
        # Backoff is real time, and these exercise the retry path several times
        # over. `_FakeClock` swaps the module's own `time` reference, so the
        # stdlib clock the asyncio loop shares is left alone.
        monkeypatch.setattr(setup_routes, "time", _FakeClock())

        class _Opener:
            def open(self, request: object, timeout: object = None) -> object:
                calls.append(len(calls))
                answer = responses[min(len(calls) - 1, len(responses) - 1)]
                if isinstance(answer, BaseException):
                    raise answer
                return answer

        monkeypatch.setattr(setup_routes, "_OPENER", _Opener())
        return calls

    @staticmethod
    def _ok(payload: bytes = b'{"ok": true}') -> object:
        class _Response:
            status = 200

            def read(self) -> bytes:
                return payload

            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

        return _Response()

    def test_a_read_timeout_is_retried_and_can_succeed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gcp shape exactly: one timeout, then the tenant answers."""
        calls = self._install(
            monkeypatch,
            [TimeoutError("The read operation timed out"), self._ok()],
        )

        status, body = self._routes().get("/api/service/configmaps/atlan-openapi")

        assert (status, body) == (200, {"ok": True})
        assert len(calls) == 2

    def test_it_gives_up_after_the_bound_and_says_what_it_saw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuine outage still fails — with the history, not just the last error."""
        calls = self._install(
            monkeypatch, [TimeoutError("The read operation timed out")]
        )

        with pytest.raises(SetupRouteError) as excinfo:
            self._routes().get("/api/service/configmaps/atlan-openapi")

        assert len(calls) == setup_routes._RETRY_ATTEMPTS
        message = str(excinfo.value)
        assert "after 3 attempts" in message
        assert "Earlier attempts" in message
        assert "TimeoutError" in message

    def test_a_404_is_answered_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The negative control's premise: a rejection is an answer.

        Retrying it would triple the control's cost for an identical verdict
        and could present a real rejection as a transient fault.
        """
        calls = self._install(
            monkeypatch,
            [
                urllib.error.HTTPError(
                    "https://tenant.example.invalid/x", 404, "Not Found", None, None
                )
            ],
        )

        status, _ = self._routes().get("/api/service/configmaps/bogus")

        assert status == 404
        assert len(calls) == 1

    @pytest.mark.parametrize("status", (400, 403, 404))
    def test_no_rejection_status_is_retried(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        """Every status the negative control accepts must cost exactly one call."""
        calls = self._install(
            monkeypatch,
            [
                urllib.error.HTTPError(
                    "https://tenant.example.invalid/x", status, "no", None, None
                )
            ],
        )

        assert self._routes().get("/x")[0] == status
        assert len(calls) == 1

    def test_a_502_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A gateway blip in front of the tenant is weather, not a verdict."""
        calls = self._install(
            monkeypatch,
            [
                urllib.error.HTTPError(
                    "https://tenant.example.invalid/x", 502, "Bad Gateway", None, None
                ),
                self._ok(),
            ],
        )

        assert self._routes().get("/x")[0] == 200
        assert len(calls) == 2

    def test_a_persistent_502_is_returned_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After the bound it is the endpoint's answer, and callers report it.

        `catalog()` turns a non-200 into "check the token has marketplace read
        access", which is a better message than a transport error for a tenant
        that is answering, just badly.
        """
        calls = self._install(
            monkeypatch,
            [
                urllib.error.HTTPError(
                    "https://tenant.example.invalid/x", 503, "nope", None, None
                )
            ],
        )

        assert self._routes().get("/x")[0] == 503
        assert len(calls) == setup_routes._RETRY_ATTEMPTS

    def test_backoff_grows_between_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Doubling, not a fixed pause — a tenant mid-restart needs the later gap."""
        clock = _FakeClock()
        monkeypatch.setattr(setup_routes, "time", clock)
        slept = clock.slept

        class _Opener:
            def open(self, request: object, timeout: object = None) -> object:
                raise TimeoutError("still out")

        monkeypatch.setattr(setup_routes, "_OPENER", _Opener())

        with pytest.raises(SetupRouteError):
            self._routes().get("/x")

        # Two waits for three attempts, and the second is longer than the first.
        assert len(slept) == setup_routes._RETRY_ATTEMPTS - 1
        assert slept[1] > slept[0]


class TestRedirectsAreDeclined:
    """`urlopen` follows redirects by default, and that default is dangerous.

    A tenant that bounces an unauthenticated request to a login page would hand
    this check a 200 carrying HTML. A check built on "200 means the form is
    served" cannot tell that from success, and the negative control — the one
    assertion that certifies all the others — is exactly what it disarms.

    These drive urllib's REAL redirect dispatch over a fake transport rather
    than over a socket. `OpenerDirector` routes a 3xx through
    `HTTPErrorProcessor` to the redirect handler exactly as it would on the
    wire, so swapping only the transport leaves the behaviour under test
    untouched — and the unit tier bans sockets (`--disable-socket` on
    Linux/macOS; the guard is skipped on Windows only because
    ProactorEventLoop needs AF_INET internally, which is why a socket-based
    version of this passed there and failed everywhere else).

    `test_the_default_handler_would_have_followed_it` is the control that makes
    the rest mean something: it shows the redirect really was there to be
    followed, so a passing "stays a 302" cannot be a fake transport that simply
    never redirected.
    """

    _LOGIN = "http://127.0.0.1:1/sso"

    @classmethod
    def _transport(cls, first_status: int) -> type[urllib.request.HTTPHandler]:
        """A transport returning *first_status*, then a 200 at the login URL.

        Both hops are answered locally, so following the redirect is fully
        observable without a network.
        """
        login = cls._LOGIN

        class _Body(io.BytesIO):
            """A response body carrying `msg`.

            `HTTPRedirectHandler.http_error_302` reads `fp.msg` when it builds
            the follow-up request, and a bare `BytesIO` cannot carry attributes
            — so without this the control below fails inside urllib rather than
            demonstrating the redirect it exists to demonstrate.
            """

            msg = "Found"

        class _Fake(urllib.request.HTTPHandler):
            def http_open(self, req: urllib.request.Request) -> object:
                if req.full_url == login:
                    headers = email.message.Message()
                    headers["Content-Type"] = "text/html"
                    return urllib.response.addinfourl(
                        _Body(b"<html>sign in</html>"), headers, req.full_url, 200
                    )
                headers = email.message.Message()
                headers["Content-Type"] = "application/json"
                if 300 <= first_status < 400:
                    headers["Location"] = login
                response = urllib.response.addinfourl(
                    _Body(b'{"ok": true}'), headers, req.full_url, first_status
                )
                # addinfourl has no declared `msg`, but HTTPErrorProcessor
                # reads one off the response when it dispatches a non-2xx.
                response.msg = "Found"  # type: ignore[attr-defined] — addinfourl has no declared `msg`; HTTPErrorProcessor reads it on a non-2xx
                return response

        return _Fake

    def _get(
        self, monkeypatch: pytest.MonkeyPatch, opener: object
    ) -> tuple[int, object]:
        from application_sdk.testing import setup_routes as module

        monkeypatch.setattr(module, "_OPENER", opener)
        routes = module.TenantRoutes.__new__(module.TenantRoutes)
        routes.base_url = "http://127.0.0.1:1"
        routes.bearer = "unused"
        return routes.get("/anything")

    def test_a_302_stays_a_302(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The finding, pinned: following it would report 200 for a login page."""
        from application_sdk.testing.setup_routes import _NoRedirect

        opener = urllib.request.build_opener(self._transport(302), _NoRedirect)

        status, _ = self._get(monkeypatch, opener)

        assert status == 302, (
            "the redirect was followed; a 302 to a login page would arrive as a "
            "200 and the negative control would certify nothing"
        )

    def test_the_default_handler_would_have_followed_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: without the override, the login page arrives as a 200.

        This is what makes the test above meaningful rather than vacuous — the
        redirect was genuinely there to be followed, and declining it is what
        changes the outcome.
        """
        opener = urllib.request.build_opener(self._transport(302))

        status, body = self._get(monkeypatch, opener)

        assert status == 200
        assert "sign in" in str(body)

    def test_a_200_is_still_a_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Declining redirects must not disturb the ordinary path."""
        from application_sdk.testing.setup_routes import _NoRedirect

        opener = urllib.request.build_opener(self._transport(200), _NoRedirect)

        status, body = self._get(monkeypatch, opener)

        assert status == 200
        assert body == {"ok": True}

    def test_the_shipped_opener_carries_the_handler(self) -> None:
        """The tests above build their own opener; this pins the real one.

        Without it they would prove `_NoRedirect` works while `TenantRoutes`
        quietly used a default opener.
        """
        from application_sdk.testing.setup_routes import _OPENER, _NoRedirect

        assert any(isinstance(h, _NoRedirect) for h in _OPENER.handlers)

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

        cards, reason = locate_cards(
            _id("clickhouse"), _eps("crawler", "miner"), catalog
        )

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
