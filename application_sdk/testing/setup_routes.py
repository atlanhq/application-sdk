"""Does every entry point's workflow-setup page actually resolve on a tenant?

FND-1667. The FND-1593 contract migration shipped a change that 404'd both of a
connector's setup pages while **every local and CI check stayed green**: the
generated artifacts were self-consistent, conformance was clean (K001/K002
cleared), and the generated-artifact freshness gate passed. Nothing was stale or
hand-edited. The break lived only in the *join* between what the contract
generates and what the tenant serves, and no gate looked there. A human opened
the UI and found it.

That is a gap, not a one-off: any contract change that moves a marketplace
card's identity breaks the setup page for a connector while leaving every
mechanical check satisfied.

What the UI does, and what this therefore asserts
-------------------------------------------------
``src/pages/workflows/setup/[id]/index.vue`` in ``atlan-frontend`` takes the
URL's ``id`` segment and spends it on two independent lookups:

* ``useMarketplaceAppById(id)`` — finds the card in
  ``GET /api/service/marketplace/apps`` by ``app.id == id``
* ``useConfigMapByName(id)`` — ``GET /api/service/configmaps/<id>`` for the
  form schema

The marketplace grid builds the link **from the card**, so the route resolves
only when the card's ``id`` is a name the configmap endpoint also answers to.
Setting ``Entrypoint.packageId`` moves the card's identity onto the marketplace
package, and the path is then derived from the package name — pointing at a name
no configmap exists for.

Resolving is not rendering
--------------------------
A 200 from that second lookup is not evidence that a user sees a form, and
FND-1680 is the proof. ``/workflows/setup/atlan-metabase`` answered 200 with a
well-formed ConfigMap envelope whose ``data.config`` was the app's
*artifact-schema declaration* — ``{"version": 1, "schemas": {...}}`` — because
the pod ran an SDK whose form-discovery rule had no ``artifact_schemas``
exclusion and served the declaration file as the setup form. The card rendered,
the request succeeded, and the form panel was blank.

So the check reads the payload rather than the status line, in the two parts the
UI actually renders from: ``properties`` defines a field and a ``steps`` panel
*draws* it. A field in one and not the other never reaches a user, and only
looking at both can tell that apart from a form that works — see
:func:`form_shortfall`.

Why it is not circular
----------------------
A check asserting ``GET configmaps/<known-good-name> == 200`` **would have
passed straight through the regression** — that name never stopped working.
What moved was the card pointing at it. So the card is located by facts that are
*not* the thing under test (the app's committed ``name``/``display_name`` and
the ``entrypoint`` name, all from ``atlan.yaml``) and only then is its ``id``
spent on the configmap endpoint.

What that ``id`` is *compared to* is the part FND-1680 corrected. Plain equality
against the generated config ``id`` is the wrong assertion for a flat app,
because ``handler.service.get_configmap`` deliberately resolves an app-id
request through a default-entrypoint fallback — see :func:`route_mismatch`. The
assertion that survives is the one that always carried the weight: fetch the
card's id, require a 200, and require the served schema to declare what this
entry point's contract declares.

Both sides of the join are the SDK's
------------------------------------
``/api/service/configmaps/<name>`` is Heracles proxying to the app pod's own
``GET /workflows/v1/configmap/{id}`` — :mod:`application_sdk.handler.service`.
So the envelope this module unwraps and the file-selection rule it applies are
read from :mod:`application_sdk.app._generated_tree`, the same authority the
server reads. Re-deriving either would compare one guess against another and
drift the moment the exclusion vocabulary grew a prefix.

Skip, don't fail
----------------
An app with no marketplace card (the behind-the-scenes pattern) and an app that
has not generated a contract both have no setup page to check. Reporting those
as failures would make this a fleet-wide false positive on its first run, so
they are skips that say why. Only a *join that is actually broken* fails.

Where it runs
-------------
This module is logic only — no argument parsing, no printing, no exit codes.
The CLI shell around it is ``verify_setup_routes.py`` in the ``sdr-e2e``
composite action, which is the one place with the SDK synced, the tenant
credentials exported and an ordering strictly after ``prepare-tenant``'s
install. Keeping the shell there and the logic here is what makes the checks
below provable offline, and it keeps this module free of the ``print`` the SDK
rightly bans.

The token never reaches this module from argv — the shell reads it from the
environment, because argv is visible in process listings and in ``set -x``
output. Nothing here logs it; failures name URLs and statuses only.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from application_sdk.app._generated_tree import form_configmap, generated_layout

__all__ = [
    "DEFAULT_CATALOG_WAIT_SECONDS",
    "AppIdentity",
    "Card",
    "Entrypoint",
    "RouteCheckSkipped",
    "RouteReader",
    "SetupRouteError",
    "TenantRoutes",
    "FormStep",
    "ServedForm",
    "declared_inputs",
    "form_shortfall",
    "locate_cards",
    "read_app_identity",
    "read_entrypoint_names",
    "read_entrypoints",
    "route_mismatch",
    "served_form",
    "verify",
]

# ---------------------------------------------------------------------------
# Endpoint paths
# ---------------------------------------------------------------------------

#: Heracles mounts its service routes under this prefix, and ``atlan-frontend``
#: builds both of these through ``getAPIPath(BASE_PATH='service', …)``. These are
#: the frontend's paths, not ours: if it changes how it derives the setup route,
#: this check goes stale. The mitigation is that it is in ONE place rather than
#: in ~79 app repos.
MARKETPLACE_APPS_PATH = "/api/service/marketplace/apps"
CONFIGMAP_PATH = "/api/service/configmaps/{name}"

_HTTP_TIMEOUT = 30
_USER_AGENT = "atlan-application-sdk-setup-routes/1.0"

#: How many times one GET is attempted before the tenant is called unreachable.
#:
#: A single read timeout against an e2e tenant is usually transient egress loss
#: on the runner, not a broken route: FND-1680's gcp leg died on
#: ``GET /api/service/configmaps/atlan-openapi ... The read operation timed
#: out`` while the azure leg passed the identical assertions against the
#: identical build, minutes apart. Failing the whole leg on one dropped packet
#: makes this check's verdict a coin flip on network weather, and a check that
#: reds for reasons unrelated to what it asserts gets ignored — which costs
#: more than the flake.
#:
#: Three, not more: the point is to survive a blip, not to wait out a genuine
#: outage. Three consecutive failures over the backoff below is evidence, and
#: the worst case stays well inside the job's budget.
_RETRY_ATTEMPTS = 3

#: Base delay between attempts, doubled each time (2s, then 4s).
_RETRY_BACKOFF_SECONDS = 2.0

#: Statuses worth a second ask. Deliberately excludes every 4xx in
#: :data:`_REJECTION_STATUSES`: those are the endpoint *answering*, and the
#: negative control's whole premise is that an unknown name is rejected
#: promptly. Retrying a 404 would turn the control into a 3x-slower way of
#: getting the same answer, and could mask a genuine rejection as a flake.
_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)

#: Statuses that count as "this name is genuinely not served". The negative
#: control accepts any of them rather than asserting which flavour of rejection
#: Heracles picks — a 400 with ``code 1000`` is its reported shape for several
#: faults, and pinning one would make the control brittle without making it
#: stronger.
_REJECTION_STATUSES = (400, 403, 404)

#: How long to keep re-reading the catalog before concluding a card is absent.
#:
#: A bounded poll rather than a single read, because ``install()`` polling the
#: *deployment* to SUCCEEDED is not evidence that the marketplace catalog and the
#: app's configmap endpoint have caught up: the catalog is LM's snapshot and the
#: configmaps are served by the pod, and nothing sequences those against the
#: deployment verdict. The prototype this generalises never exercised the
#: immediately-post-install path — every one of its runs read a tenant where the
#: app had been installed earlier — so a single read here would be
#: flaky-by-construction on exactly the path CI takes.
DEFAULT_CATALOG_WAIT_SECONDS = 120
_CATALOG_POLL_SECONDS = 10

#: How many catalog entries the not-found diagnostic names verbatim.
#:
#: The catalog runs to ~140 cards on a live tenant, so the whole list would push
#: the real finding off the top of a CI log. Twenty is enough to recognise a
#: catalog and to see the shape its ``name`` fields take.
_CARD_SAMPLE = 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Declines every redirect instead of following it.

    Returning ``None`` from ``redirect_request`` tells urllib the response was
    not handled, so the 3xx surfaces as an :class:`~urllib.error.HTTPError`
    carrying its real status rather than being replaced by whatever the
    redirect target returns.

    This exists because the failure it prevents is invisible: a tenant that
    bounces an unauthenticated request to a login page would otherwise hand
    this check a 200, and a check built on "200 means the form is served"
    cannot tell that from success.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


#: Built once: an opener is stateless here and rebuilding it per request would
#: re-parse the handler chain on every call.
_OPENER = urllib.request.build_opener(_NoRedirect())


class RouteReader(Protocol):
    """The two reads :func:`verify` needs from a tenant.

    Declared as a Protocol rather than typed against :class:`TenantRoutes`
    because ``verify`` genuinely needs only these two methods — the transport,
    the credential and the redirect policy are all `TenantRoutes`' business and
    none of them reaches this far.

    It is also what keeps the test suite free of suppressions. Annotating the
    parameter with the concrete class meant every test passing a fake needed a
    ``# type: ignore[arg-type]``, and fourteen identical suppressions are worse
    than one honest signature: each one is a place where a genuine type error
    would be silenced too.
    """

    def catalog(self) -> list[dict[str, Any]]:
        """Every marketplace card the tenant lists."""
        ...

    def configmap(self, name: str) -> tuple[int, dict[str, Any]]:
        """Fetch one configmap by name, as the setup page does."""
        ...


class SetupRouteError(RuntimeError):
    """A setup route is broken, or the tenant could not be asked."""


class RouteCheckSkipped(RuntimeError):
    """There is no setup route to check, and that is not a failure.

    Raised for an app with no marketplace card and for one that has not
    generated a contract. Carries the reason so the caller can say why it
    skipped instead of going quietly green.
    """


# ---------------------------------------------------------------------------
# Contract side — read from the committed artifacts, never hardcoded
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entrypoint:
    """One entry point, as the committed artifacts describe it.

    **Never carries an empty name.** An earlier revision keyed a flat app's
    single contract under ``""``, which quietly became a magic value doing two
    unrelated jobs — "this app declares no entrypoints" and "match a card whose
    entrypoint field is blank" — and made a healthy single-card app report as
    having no card at all. Every entry point here has a real, committed,
    non-empty label, and every one is validated explicitly.
    """

    name: str
    """The label this entry point is checked and reported under; never empty.

    For an app declaring ``entrypoints[]``, the kebab-case wire name from
    ``atlan.yaml`` — which is also its directory in the generated tree and the
    ``entrypoint`` its marketplace card carries.

    For an app declaring none, the generated config's own ``id`` (``mysql``).
    That is a committed fact rather than a placeholder, and it keeps the label
    meaningful in a message without inventing a name the app never wrote down.
    """

    config_id: str
    """``id`` from the generated workflow config — the name the form is served
    under, and therefore the name the card must point at."""

    is_sole: bool = False
    """``True`` when the app declares no ``entrypoints[]``, so this is its one
    contract and its one card.

    Load-bearing for card matching, and the reason no default has to be
    calculated: with one card there is nothing to choose between, so the card's
    own ``entrypoint`` field — whatever the catalog wrote there — takes no part
    in the match. A bundle's cards are always matched by name.
    """

    declared: frozenset[str] = frozenset()
    """Input names the committed contract declares for this entry point."""

    source: Path | None = None
    """The generated file that answered, for diagnostics."""


@dataclass(frozen=True)
class AppIdentity:
    """The committed names a marketplace card may be listed under.

    **Two, not one, because the catalog is not consistent about which it
    stores.** The first fleet-wide run read one live tenant and found openapi
    and metabase listed under their wire ``name`` while mysql was listed under
    its ``display_name`` (``MySQL Assets``) — so a locator keyed on either
    field alone reports a healthy, installed app as absent from the catalog,
    which is the most misleading message this check can emit.

    Both values are committed in ``atlan.yaml`` and neither is the thing under
    test, so widening the *locator* to accept either leaves the non-circularity
    argument in this module's header intact: the card is still found by facts
    that are not the card's ``id``, and only then is its ``id`` examined.
    """

    name: str
    """``atlan.yaml``'s top-level ``name``, normalised. Never empty."""

    display_name: str = ""
    """``atlan.yaml``'s ``display_name``, normalised. Empty when not declared."""

    @property
    def labels(self) -> tuple[str, ...]:
        """Every name a card may legitimately be listed under, normalised.

        Deduplicated and empties dropped, so an app whose ``display_name``
        equals its ``name`` — or declares none — yields exactly one label
        rather than a doubled or blank one.
        """
        seen: list[str] = []
        for label in (self.name, self.display_name):
            if label and label not in seen:
                seen.append(label)
        return tuple(seen)

    def matches(self, card: Card) -> bool:
        """Whether *card*'s ``name`` is one of this app's committed labels.

        Case-folded and stripped on both sides. ``read_app_identity``
        normalises this side, but a card's ``name`` is whatever the catalog
        stores, and comparing the two verbatim reports a mixed-case card as
        "this app is not installed on this tenant".
        """
        return card.name.strip().lower() in self.labels


def read_app_identity(repo_root: Path) -> AppIdentity:
    """Return the names ``atlan.yaml`` commits, as cards may report them.

    ``name`` is the field the fleet is already forced to commit —
    ``parse_atlan_yaml.py`` reads it on every release — and ``display_name``
    is emitted beside it by the same generator. Read here rather than threaded
    in from a step output so the check has one source for both.

    Raises:
        SetupRouteError: when ``atlan.yaml`` is unreadable, is invalid YAML, or
            declares no top-level ``name``.
    """
    import yaml  # noqa: PLC0415 — deferred so an import of this module is cheap

    path = repo_root / "atlan.yaml"
    try:
        parsed = yaml.safe_load(path.read_text()) or {}
    except OSError as exc:
        raise SetupRouteError(
            f"cannot read {path}: {exc}. The route check runs in the app repo's "
            "working directory and needs its committed atlan.yaml."
        ) from exc
    except yaml.YAMLError as exc:
        raise SetupRouteError(f"{path} is invalid YAML: {exc}") from exc

    name = str(parsed.get("name") or "").strip().lower()
    if not name:
        raise SetupRouteError(f'{path} has no top-level "name"')
    return AppIdentity(
        name=name,
        display_name=str(parsed.get("display_name") or "").strip().lower(),
    )


def read_entrypoint_names(repo_root: Path) -> list[str]:
    """Return the ``entrypoints[].name`` values that should have a setup page.

    Empty is meaningful, not an error: a single-generated-contract app declares
    no ``entrypoints`` block and is checked as one flat entry point keyed ``""``.

    An entry point carrying ``marketplace_card: false`` is left out. That is the
    behind-the-scenes pattern — an entry point the DAG invokes with no card for
    a user to click — and it has no setup page to resolve, so asserting one
    exists would be a false positive rather than a finding.

    The **absence** of the key is not read as "no card", deliberately: live
    connectors are cards today while emitting neither ``marketplace_card`` nor
    ``package_id`` (the key only appears once ``packageId`` is set), so keying
    on absence would skip the whole fleet and this check would assert nothing.
    Only an explicit ``false`` opts out. FND-1659 is resolving what card
    presence should key on; if it lands a clearer signal, this is the one place
    that changes.
    """
    import yaml  # noqa: PLC0415 — see read_app_identity

    parsed = yaml.safe_load((repo_root / "atlan.yaml").read_text()) or {}
    declared = parsed.get("entrypoints")
    if not isinstance(declared, list):
        return []
    return [
        str(entry["name"]).strip()
        for entry in declared
        if isinstance(entry, dict)
        # A blank or whitespace-only name is dropped rather than carried: it
        # cannot name a directory, cannot match a card, and would reintroduce
        # the empty-string key this module deliberately has no place for.
        and str(entry.get("name") or "").strip()
        and entry.get("marketplace_card", True) is not False
    ]


def _declares_any_entrypoint(repo_root: Path) -> bool:
    """Whether ``atlan.yaml`` declares an ``entrypoints`` list with any entry.

    Distinguishes "declares entrypoints, all of which opted out of a card" from
    "declares no entrypoints at all" — a skip and a failure respectively, which
    :func:`read_entrypoint_names` cannot tell apart on its own.
    """
    import yaml  # noqa: PLC0415 — see read_app_identity

    parsed = yaml.safe_load((repo_root / "atlan.yaml").read_text()) or {}
    declared = parsed.get("entrypoints")
    return isinstance(declared, list) and bool(declared)


def declared_inputs(workflow_config: dict[str, Any]) -> frozenset[str]:
    """Return the input names a generated workflow config declares.

    ``config.properties`` is a flat dict keyed by the kebab-case input name.
    Its siblings are deliberately not read: ``steps`` is the wizard
    progression and ``anyOf`` the conditional-visibility rules, so neither is
    the input set.
    """
    config = workflow_config.get("config")
    if not isinstance(config, dict):
        return frozenset()
    properties = config.get("properties")
    if not isinstance(properties, dict):
        return frozenset()
    return frozenset(str(key) for key in properties)


def read_entrypoints(
    repo_root: Path, generated_dir: str = "app/generated"
) -> list[Entrypoint]:
    """Read every entry point's committed workflow config.

    The entry-point *names* come from ``atlan.yaml`` and the *files* are located
    through :func:`application_sdk.app._generated_tree.form_configmap`, so this
    agrees with the endpoint that serves them by construction rather than by a
    second glob that could drift.

    Raises:
        RouteCheckSkipped: when the app has generated no contract — there is no
            setup page for a tree that does not exist.
        SetupRouteError: when a declared entry point has no generated config, or
            its config carries no ``id``. Both mean the committed artifacts are
            incoherent, which is a real failure rather than nothing to check.
    """
    generated = repo_root / generated_dir
    layout = generated_layout(generated)
    if layout == "unknown":
        raise RouteCheckSkipped(
            f"no generated contract under {generated} (no manifest.json at its "
            "root or in any immediate subdirectory), so this app has no setup "
            "form to serve and no route to check. Generate the contract if this "
            "is unexpected."
        )

    sole = layout != "multi"
    # One pass for a sole contract. `form_configmap` ignores the entrypoint
    # for a non-`multi` layout, and the Entrypoint built below is labelled by
    # its committed config id — so this value names nothing and is never a key.
    names = ["<sole>"] if sole else read_entrypoint_names(repo_root)
    if layout == "multi" and not names:
        # Two different situations that must not share an outcome: an app whose
        # entrypoints all declare `marketplace_card: false` has nothing to check
        # (skip), while one declaring no entrypoints at all under a bundle
        # layout is incoherent (fail). read_entrypoint_names returns an empty
        # list for both.
        if _declares_any_entrypoint(repo_root):
            raise RouteCheckSkipped(
                f"every entrypoint in {repo_root / 'atlan.yaml'} declares "
                "marketplace_card: false, so none has a setup page for a user "
                "to open and there is no route to check."
            )
        raise SetupRouteError(
            f"{generated} has a bundle layout (per-entry-point subdirectories) "
            "but atlan.yaml declares no entrypoints, so no entry point's card "
            "can be located. One of the two is wrong."
        )

    found: list[Entrypoint] = []
    for name in names:
        path = form_configmap(generated, name, layout=layout)
        if path is None:
            raise SetupRouteError(
                f"entrypoint {name!r} is declared in atlan.yaml but "
                f"{generated / name if name else generated} holds no setup-form "
                "configmap (only a manifest and/or credential templates). "
                "Regenerate the contract."
            )
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SetupRouteError(f"cannot read {path}: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("id"):
            raise SetupRouteError(
                f"{path} carries no top-level 'id', so there is no name for the "
                "marketplace card to point at. Regenerate the contract."
            )
        config_id = str(payload["id"])
        found.append(
            Entrypoint(
                # A sole contract is labelled by its own committed config id, so
                # no entry point here ever carries an empty name.
                name=config_id if sole else name,
                config_id=config_id,
                is_sole=sole,
                declared=declared_inputs(payload),
                source=path,
            )
        )
    return found


# ---------------------------------------------------------------------------
# The pure checks — no network, so they are provable offline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    """One marketplace card, trimmed to the fields the check reads."""

    id: str = ""
    name: str = ""
    entrypoint: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Card:
        return cls(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            entrypoint=str(payload.get("entrypoint") or ""),
        )


def route_mismatch(entrypoint: Entrypoint, card: Card) -> str | None:
    """Why this entry point's setup page will 404, or ``None`` if it resolves.

    Pure, so the assertion it backs is provable without a tenant — see
    ``tests/unit/testing/test_setup_routes.py``, which feeds it the card shape
    the FND-1593 regression produced and asserts it reports the break naming
    both sides.

    **A card id that differs from the config id is not, by itself, a break.**
    ``handler.service.get_configmap`` resolves an unmatched id through a
    documented *default-entrypoint fallback*: "the marketplace UI sometimes
    builds the configmap URL from the app/marketplace id (e.g.
    ``atlan-snowflake``) rather than the entrypoint-form stem". That fallback
    exists because ``atlan-openapi`` really did 404 in production, and it has a
    committed regression test. An earlier revision of this function asserted
    plain equality and therefore failed every flat connector on the fleet's
    first run — openapi, metabase and mysql all serve a card id of
    ``atlan-<name>`` against a config id of ``<name>``, and every one of those
    setup pages renders (FND-1680).

    So the tolerance is **gated on** :attr:`Entrypoint.is_sole`, not granted
    generally, because that is precisely where the fallback is safe: it
    resolves to the app's *default* entry point, which for a sole contract is
    the only entry point there is. For a bundle the same fallback would serve
    the default entry point's form for *every* card, and the equality check is
    the only thing standing between that and a user opening one entry point's
    page to find another's form — so a bundle still fails on any mismatch.

    A tolerated mismatch is not waved through: :func:`verify` still fetches
    ``configmaps/<card.id>`` and still requires the served schema to carry this
    entry point's declared inputs. Equality was only ever a proxy for that, and
    it is the wrong proxy on the server the check is asserting about.

    Args:
        entrypoint: The entry point as the committed artifacts describe it.
        card: The card the tenant serves, located by app name + entrypoint.
    """
    if not card.id:
        return (
            f"Entrypoint {entrypoint.name!r}: the marketplace card has no "
            "id, so the marketplace cannot build a setup link for it at all."
        )
    if not entrypoint.config_id:
        return (
            f"Entrypoint {entrypoint.name!r}: the generated workflow "
            "config has no id. Regenerate the contract."
        )
    if card.id != entrypoint.config_id and not entrypoint.is_sole:
        return (
            f"Entrypoint {entrypoint.name!r}: the marketplace card's id "
            f"is {card.id!r}, but this repo generates its workflow config as "
            f"{entrypoint.config_id!r}. This app is a bundle, so the app-id "
            "fallback in handler.service.get_configmap would resolve that name "
            "to the DEFAULT entry point's form — meaning /workflows/setup/"
            f"{card.id} either 404s or renders another entry point's form. A "
            "contract change that moves the card id — setting `packageId` on "
            "the entrypoint is the known one (FND-1659) — is the cause."
        )
    return None


def form_shortfall(entrypoint: Entrypoint, served: ServedForm) -> str | None:
    """Why this entry point's setup form will not render, or ``None``.

    The question a 200 cannot answer. FND-1680's metabase page returned 200 for
    ``atlan-metabase`` and rendered **blank**: the app pod served its
    ``artifact_schemas.json`` as the setup form, so ``data.config`` carried
    ``{"version": 1, "schemas": {...}}`` — no ``properties``, no ``steps``, and
    nothing for the wizard to draw. Every status-code assertion passed.

    Four distinct faults, reported in the order that makes the *cause* legible
    rather than the symptom:

    #. the payload is not a setup form at all — no fields and no panels;
    #. fields the contract declares are absent from the served schema;
    #. the schema has fields but no panels, so none of them is drawn;
    #. a declared field exists but no panel names it, so it never appears.

    A **subset** check throughout, never equality: the platform may decorate the
    schema with fields the contract never named, and failing on those buys no
    safety.

    Args:
        entrypoint: The entry point, carrying the inputs its contract declares.
        served: What the tenant actually returned, parsed by :func:`served_form`.
    """
    label = entrypoint.name
    declared = entrypoint.declared
    if not declared:
        return (
            f"Entrypoint {label!r}: the committed workflow "
            "config declares no inputs at all, so this check cannot tell a "
            "served form from an empty one. Regenerate the contract."
        )

    if not served.properties and not served.steps:
        return (
            f"Entrypoint {label!r}: the endpoint answered 200 but the payload "
            "is not a setup form — it carries neither 'properties' nor "
            "'steps', so the page renders blank. The known cause is the app "
            "pod resolving this name to a non-form sibling in app/generated "
            "(artifact_schemas.json sorts first, FND-1680); an app running an "
            "SDK without that exclusion serves the declaration file as its "
            f"form. This repo's contract declares {sorted(declared)}."
        )

    missing = declared - served.properties
    if missing:
        return (
            f"Entrypoint {label!r}: the tenant's form schema is "
            f"missing {sorted(missing)}, which this repo's committed contract "
            "declares. Most likely the tenant runs an older image than this "
            "branch; it can also mean a contract change never reached the "
            f"deployed app. Served: {sorted(served.properties)}."
        )

    if not served.steps:
        return (
            f"Entrypoint {label!r}: the served schema defines "
            f"{len(served.properties)} fields but declares no 'steps', so the "
            "wizard has no panels to draw and the page renders blank. Fields "
            "exist in the schema and none of them reaches a user."
        )

    unrendered = declared - served.rendered
    if unrendered:
        return (
            f"Entrypoint {label!r}: {sorted(unrendered)} are defined in the "
            "served schema but named by no step, so the wizard never draws "
            "them — a user cannot fill in a field that is not on any panel. "
            f"Steps: {[step.id for step in served.steps]}."
        )
    return None


def _render(cards: Sequence[Card]) -> str:
    """The read fields of a bounded sample of *cards*, for a failure message."""
    return "; ".join(
        f"id={card.id!r} name={card.name!r} entrypoint={card.entrypoint!r}"
        for card in cards[:_CARD_SAMPLE]
    )


def _catalog_evidence(identity: AppIdentity, cards: Sequence[Card]) -> str:
    """What the catalog actually carried, for a lookup that matched no card.

    FND-1680. The first fleet-wide run of this check reported ``no marketplace
    card has name='mysql'. The tenant listed 139 apps`` and then waited out the
    full reconcile poll — on a tenant the *preceding* step had already proven
    was running the app at the version under test. So either the card genuinely
    is absent, or ``name`` is not the field carrying the app's name, and the
    message as written could not tell those apart. It logged the card *count*
    and never a single card, so nothing in the log said which field held
    ``mysql``.

    This answers it in the failure itself rather than in the next
    investigation, in three descending tiers of certainty:

    #. some card carries a committed label **exactly** in ``id`` or ``entrypoint`` —
       the app is in the catalog and this lookup reads the wrong field;
    #. some card carries it as a **substring** of any read field — most likely
       the same app under a decorated identity, so the card is shown;
    #. nothing carries it at all — a genuine absence, and a bounded sample of
       the names present is what a reader needs to confirm it.

    Kept after it did its job, not removed with the bug. Its first live run is
    what showed mysql's card listed under its ``display_name`` — the finding
    :class:`AppIdentity` now encodes — and the next catalog-shape surprise will
    arrive the same way: as a lookup that matches nothing and has to explain
    itself. This is the part of the check that stays useful once the bug it
    diagnosed is fixed.

    Args:
        identity: The app's committed, already-normalised labels.
        cards: Every card the tenant listed.
    """
    labels = identity.labels
    # One pass, and a list rather than a dict keyed by Card: a catalog serving
    # the same card twice is a fault worth *seeing* in the sample, and a dict
    # would silently collapse the duplicate into one.
    exact: list[Card] = []
    partial: list[Card] = []
    for card in cards:
        values = (card.id.strip().lower(), card.entrypoint.strip().lower())
        if any(label in values for label in labels):
            exact.append(card)
        elif any(
            label in value
            for label in labels
            for value in (*values, card.name.strip().lower())
        ):
            # Substring, not exact: a card named `atlan-mysql` or
            # `mysql-crawler` is almost certainly this app under a decorated
            # identity, and printing it beats printing an alphabetical slice of
            # the catalog that may not reach it — 20 of 139 sorted names is a
            # 1-in-7 chance of covering the one that matters.
            partial.append(card)

    if exact:
        return (
            f"But {len(exact)} card(s) carry one of {list(labels)} exactly, in "
            f"'id' or 'entrypoint': {_render(exact)}. So the app IS in the "
            "catalog and this lookup is reading the wrong field — not an "
            "install that never landed."
        )

    if partial:
        return (
            f"No card carries any of {list(labels)} exactly in a read field, but "
            f"{len(partial)} carry one as a substring: {_render(partial)}. Either "
            "the catalog decorates this app's identity or another app's name "
            "contains it; the triples say which."
        )

    names = sorted({card.name for card in cards if card.name})
    return (
        f"No card carries any of {list(labels)} in 'id', 'name' or 'entrypoint', "
        "even as a substring, so this app's build is most likely not installed "
        "on this tenant rather than this lookup reading the wrong field. Card "
        f"names ({min(len(names), _CARD_SAMPLE)} of {len(names)}, sorted): "
        f"{names[:_CARD_SAMPLE]}."
    )


def locate_cards(
    identity: AppIdentity,
    entrypoints: Sequence[Entrypoint],
    payloads: list[dict[str, Any]],
) -> tuple[dict[str, Card], str | None]:
    """Pick this app's cards out of the whole catalog, keyed by entry point.

    Matched on one of the app's committed names (:attr:`AppIdentity.labels`)
    **and** ``entrypoint``. Both are required: an ``entrypoint`` alone is not
    app-scoped — every connector's crawler card carries ``entrypoint:
    "crawler"``, so filtering on it alone matched a dozen unrelated apps when
    the prototype tried it, and the count was plausible enough to look like
    success.

    **A sole contract is matched by count, not by label.** An app declaring no
    ``entrypoints[]`` has one form and one card, so there is nothing to choose
    between and the card's ``entrypoint`` field takes no part in the match —
    which is what lets a route/card-split app, whose single card is served
    carrying one of its several route names, resolve correctly.

    That is also why no default entry point is calculated anywhere in this
    module. ``@entrypoint(default=True)`` is a runtime ``AppRegistry`` flag
    absent from both ``atlan.yaml`` and ``manifest.json``, so any default
    computed here would be a presumption — and every case that might have
    needed one is answered by a fact instead: a bundle matches by name, and a
    sole contract matches because it is the only one.

    Returns:
        The cards found, and a reason string when one or more declared entry
        points has no card (``None`` when every one resolved).
    """
    # Materialised rather than streamed: the not-found path below reads every
    # card a second time to say what the catalog *did* carry, and a generator
    # would be exhausted by the filter — leaving the diagnostic that exists to
    # explain the miss with nothing to explain it from.
    catalog = [Card.from_payload(p) for p in payloads]
    ours = [card for card in catalog if identity.matches(card)]
    if not ours:
        return {}, (
            f"no marketplace card has name in {list(identity.labels)}. The "
            f"tenant listed {len(payloads)} apps, so the catalog is readable. "
            f"{_catalog_evidence(identity, catalog)}"
        )

    # A flat generated tree means ONE card, and its `entrypoint` is the tenant's
    # business rather than ours: a route/card-split app has several
    # `@entrypoint`s behind a single card, and that card is served carrying one
    # of their names (`entrypoint: "crawler"`), not the empty string this check
    # keys a flat tree under. Matching on the key would report a perfectly
    # healthy single-card app as having no card at all.
    #
    # So for a flat tree the entrypoint is not part of the match — the app name
    # already identified the card, and there is only one to identify.
    if len(entrypoints) == 1 and entrypoints[0].is_sole:
        sole = entrypoints[0]
        if len(ours) > 1:
            return {}, (
                f"{identity.name!r} declares no entrypoints and generates one setup "
                f"form, but the tenant lists {len(ours)} cards for it "
                f"(entrypoints: {sorted(card.entrypoint for card in ours)}). "
                "Which card's id the setup route should match is ambiguous, so "
                "this reports rather than guessing — most likely the generated "
                "tree and the installed build disagree about the app's shape."
            )
        return {sole.name: ours[0]}, None

    by_entrypoint = {card.entrypoint: card for card in ours if card.entrypoint}
    names = [entrypoint.name for entrypoint in entrypoints]

    unlabelled = [card for card in ours if not card.entrypoint]
    if unlabelled:
        # Reported, never attributed. This app declares its entrypoints by name,
        # so a card carrying none cannot be assigned to one without a guess —
        # and a guess here produces a spurious "the card points at the wrong
        # form" that reads exactly like a real contract break.
        return by_entrypoint, (
            f"{identity.name!r} declares entrypoints {sorted(names)}, but the tenant "
            f"serves {len(unlabelled)} card(s) for it carrying no entrypoint "
            f"(ids: {sorted(card.id for card in unlabelled)}). An unlabelled "
            "card cannot be attributed to a named entrypoint, so this reports "
            "it rather than presuming one. The usual cause is an installed "
            "build that predates the entrypoint split, serving one card where "
            "this branch generates several."
        )

    missing = [name for name in names if name not in by_entrypoint]
    if missing:
        return by_entrypoint, (
            f"{identity.name!r} generates entrypoints {sorted(names)} but the "
            f"tenant lists cards for {sorted(by_entrypoint)}. Missing: "
            f"{sorted(missing)} — an entrypoint with no card has no setup page "
            "at all. Either the installed build predates it, or the catalog "
            "stopped expanding entrypoint cards."
        )
    return by_entrypoint, None


@dataclass(frozen=True)
class FormStep:
    """One panel of the setup wizard, as the served schema describes it."""

    id: str
    """The step's own ``id`` (``credential``, ``connection``, ``metadata``), or
    a positional placeholder when the served step declares none."""

    properties: frozenset[str] = frozenset()
    """The field names this panel puts on screen."""


@dataclass(frozen=True)
class ServedForm:
    """The setup form a tenant serves, in the two parts the UI renders from.

    **A 200 is not evidence that a user sees a form.** FND-1680's metabase case
    answered 200 with ``metadata.name: atlan-metabase`` and a ``data.config``
    holding the *artifact-schema declaration* — ``{"version": 1, "schemas":
    {...}}``, no ``properties`` and no ``steps`` — and the setup page rendered
    blank while every status-code assertion passed. So the check reads what the
    payload actually contains rather than trusting the code that carried it.

    Two parts, because a field needs both to reach a user: ``properties``
    defines it and a step *draws* it. Reading only the first is what let a
    served-but-invisible field look identical to a rendered one.
    """

    properties: frozenset[str] = frozenset()
    """Field names the schema defines. Necessary for a field to exist."""

    steps: tuple[FormStep, ...] = ()
    """The wizard's panels, in order. Necessary for a field to be drawn."""

    @property
    def rendered(self) -> frozenset[str]:
        """Every field some step actually puts on screen.

        The union over *steps*, not over ``properties``: a field the schema
        defines but no step names is served and never seen, which to the user
        filling the form in is indistinguishable from missing.
        """
        rendered: frozenset[str] = frozenset()
        return rendered.union(*(step.properties for step in self.steps))


def served_form(body: dict[str, Any]) -> ServedForm:
    """Unwrap a ConfigMap response into the form the tenant serves.

    The endpoint answers with a K8s ConfigMap whose ``data.config`` is the form
    schema as a JSON **string**, not a nested object — see
    ``handler.service.get_configmap``, which builds it as
    ``{"config": dumps(raw.get("config", raw))}``. The parsed value is therefore
    the committed file's ``config`` VALUE: one nesting level shallower than the
    committed file, which is the easiest thing to get wrong here.

    Missing or malformed ``properties``/``steps`` degrade to empty rather than
    raising. "The endpoint served something that is not a setup form" is a
    finding for :func:`form_shortfall` to report *with the evidence*, not a
    crash inside the parser. Only a broken **envelope** — no ``data``, no
    string ``data.config``, unparseable JSON — raises here, because that means
    the endpoint's own contract changed and nothing downstream can be trusted.
    """
    data = body.get("data")
    if not isinstance(data, dict):
        raise SetupRouteError(
            "configmap response carries no 'data' object; the endpoint's "
            f"response shape has changed (keys: {sorted(body)})"
        )
    raw = data.get("config")
    if not isinstance(raw, str):
        raise SetupRouteError(
            "configmap response has no string data.config; got "
            f"{type(raw).__name__}. The endpoint's response shape has changed."
        )
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SetupRouteError(f"data.config is not valid JSON: {exc}") from exc
    if not isinstance(schema, dict):
        return ServedForm()

    properties = schema.get("properties")
    names = (
        frozenset(str(key) for key in properties)
        if isinstance(properties, dict)
        else frozenset()
    )

    raw_steps = schema.get("steps")
    steps: list[FormStep] = []
    for index, step in enumerate(raw_steps if isinstance(raw_steps, list) else []):
        if not isinstance(step, dict):
            continue
        declared = step.get("properties")
        steps.append(
            FormStep(
                id=str(step.get("id") or f"<step {index}>"),
                properties=frozenset(str(name) for name in declared)
                if isinstance(declared, list)
                else frozenset(),
            )
        )
    return ServedForm(properties=names, steps=tuple(steps))


# ---------------------------------------------------------------------------
# Tenant side
# ---------------------------------------------------------------------------


@dataclass
class TenantRoutes:
    """Reads the two routes the setup page walks, with one bearer credential."""

    base_url: str
    bearer: str = field(repr=False)

    def __post_init__(self) -> None:
        candidate = self.base_url.strip().rstrip("/")
        parsed = urllib.parse.urlparse(candidate)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SetupRouteError(
                f"invalid tenant base URL {self.base_url!r}: expected a bare "
                "https://<host> with no userinfo, query, or fragment."
            )
        self.base_url = candidate

    def get(self, path: str) -> tuple[int, object]:
        """One GET. Returns ``(status, parsed_body)``; non-2xx is returned.

        Redirects are declined rather than followed, via :class:`_NoRedirect`.
        ``urlopen`` follows them by default, and that default is actively
        dangerous here: a 302 to a login page would arrive as a **200 carrying
        an HTML login form**, which turns an auth failure into a success. The
        negative control would then see a "200" for a config name that cannot
        exist and conclude the endpoint does not discriminate — except it never
        gets that far, because a 200-shaped login page fails the JSON parse
        first. Either way the failure is about the redirect and reads as
        something else entirely.

        A declined redirect surfaces as its real 3xx status, which no caller
        treats as success and which the catalog read reports as a token
        problem — the actual cause.
        """
        request = urllib.request.Request(  # noqa: S310 — https base validated in __post_init__
            f"{self.base_url}{path}", method="GET"
        )
        request.add_header("Authorization", f"Bearer {self.bearer}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", _USER_AGENT)

        # Retried attempts are recorded rather than printed, and spent in the
        # failure message below. This class has no progress callback and adding
        # one would change its constructor — which the `@main`-pinned CLI shell
        # calls against each app's OWN pinned SDK, so a new required argument
        # is a fleet-wide TypeError the moment it lands. Putting the history in
        # the error puts it exactly where a reader is already looking.
        earlier: list[str] = []
        started = time.monotonic()
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            last = attempt == _RETRY_ATTEMPTS
            try:
                with _OPENER.open(  # noqa: S310 — see above
                    request, timeout=_HTTP_TIMEOUT
                ) as response:
                    return response.status, _parse(response.read())
            except urllib.error.HTTPError as exc:
                # A declined redirect lands here too, carrying its own 3xx
                # status: `redirect_request` returning None makes urllib treat
                # the response as unhandled, and the error processor raises it.
                # Those are answers, not faults, and are returned as-is.
                if last or exc.code not in _RETRYABLE_STATUSES:
                    return exc.code, _parse(exc.read())
                earlier.append(f"HTTP {exc.code}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if last:
                    raise SetupRouteError(
                        f"GET {path} could not reach {self.base_url} after "
                        f"{_RETRY_ATTEMPTS} attempts over "
                        f"{time.monotonic() - started:.0f}s: {exc}. Earlier "
                        f"attempts: {earlier or ['none']}. One read timeout "
                        "here is usually transient egress loss on the runner "
                        f"rather than a broken route, which is why this "
                        f"retries; {_RETRY_ATTEMPTS} in a row is not."
                    ) from exc
                earlier.append(f"{type(exc).__name__}: {exc}")
            time.sleep(_RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))

        # Unreachable: the loop's final attempt always returns or raises. Kept
        # so a future edit to the bounds cannot fall off the end into `None`.
        raise SetupRouteError(f"GET {path}: retry loop exhausted unexpectedly")

    def catalog(self) -> list[dict[str, Any]]:
        """Every marketplace card the tenant lists."""
        status, body = self.get(MARKETPLACE_APPS_PATH)
        if status != 200 or not isinstance(body, dict):
            raise SetupRouteError(
                f"GET {MARKETPLACE_APPS_PATH} returned {status}; cannot resolve "
                "marketplace cards. Check the token has marketplace read access."
            )
        apps = body.get("apps")
        if not isinstance(apps, list):
            raise SetupRouteError(
                f"GET {MARKETPLACE_APPS_PATH} returned no 'apps' list "
                f"(keys: {sorted(body)}); the response shape has changed."
            )
        # A truncated catalog reads as "this app is not installed" — a silent
        # wrong answer, and the worst possible failure for a check whose value
        # is that it does not false-positive. The endpoint reports `total`
        # alongside the page, and no pagination parameter was needed at the
        # ~140-app scale this was confirmed at; if a page limit is ever
        # introduced, this turns a plausible-looking miss into a loud one
        # rather than leaving the check quietly reading a prefix.
        total = body.get("total")
        if isinstance(total, int) and total > len(apps):
            raise SetupRouteError(
                f"GET {MARKETPLACE_APPS_PATH} reported total={total} but "
                f"returned only {len(apps)} cards, so the catalog is paginated "
                "and this read saw a prefix of it. An app missing from a "
                "truncated page is indistinguishable from one that is not "
                "installed, so this fails rather than guessing. The reader "
                "needs a pagination parameter."
            )
        return [entry for entry in apps if isinstance(entry, dict)]

    def configmap(self, name: str) -> tuple[int, dict[str, Any]]:
        """Fetch one configmap by name, exactly as the setup page does."""
        status, body = self.get(
            CONFIGMAP_PATH.format(name=urllib.parse.quote(name, safe=""))
        )
        return status, body if isinstance(body, dict) else {}


def _parse(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw.decode(errors="replace")


def _await_cards(
    routes: RouteReader,
    identity: AppIdentity,
    entrypoints: Sequence[Entrypoint],
    wait_seconds: int,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Card]:
    """Poll the catalog until every entry point has a card, or give up.

    See :data:`DEFAULT_CATALOG_WAIT_SECONDS` for why this polls rather than
    reading once. The LAST reason is raised, not the first: an early read that
    saw no card at all is less informative than a late one that saw all but one.
    """
    deadline = time.monotonic() + max(wait_seconds, 0)
    reason = "the catalog was never read"
    while True:
        cards, reason_now = locate_cards(identity, entrypoints, routes.catalog())
        if reason_now is None:
            return cards
        reason = reason_now
        if time.monotonic() >= deadline:
            raise SetupRouteError(
                f"{reason} Waited {wait_seconds}s for the marketplace catalog to "
                "reconcile after the install."
            )
        if on_progress is not None:
            on_progress(
                f"catalog not ready ({reason_now}); "
                f"retrying in {_CATALOG_POLL_SECONDS}s"
            )
        time.sleep(_CATALOG_POLL_SECONDS)


def _check_route(
    routes: RouteReader, entrypoint: Entrypoint, card: Card
) -> tuple[str | None, ServedForm | None]:
    """Assert one entry point's setup route. Returns ``(failure, form)``.

    Exactly one of the two is ever set: a reason the page is broken, or the
    form the tenant served. Split out of :func:`verify` so :func:`_await_route`
    can run it repeatedly without duplicating the assertion.
    """
    mismatch = route_mismatch(entrypoint, card)
    if mismatch is not None:
        return mismatch, None

    status, body = routes.configmap(card.id)
    if status != 200:
        return (
            f"Entrypoint {entrypoint.name!r}: GET "
            f"{CONFIGMAP_PATH.format(name=card.id)} returned {status}. That "
            f"is the request /workflows/setup/{card.id} makes to render its "
            "form, so a non-200 here is a 404'd setup page for a user."
        ), None

    served_name = (body.get("metadata") or {}).get("name")
    if served_name != card.id:
        return (
            f"Entrypoint {entrypoint.name!r}: asked the configmap endpoint for "
            f"{card.id!r} and it served {served_name!r}. The setup page "
            "would render another entry point's form."
        ), None

    form = served_form(body)
    shortfall = form_shortfall(entrypoint, form)
    if shortfall is not None:
        return shortfall, None
    return None, form


def _await_route(
    routes: RouteReader,
    entrypoint: Entrypoint,
    card: Card,
    wait_seconds: int,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[str | None, ServedForm | None]:
    """Poll one entry point's route until it resolves, or give up saying why.

    **The install verdict is not the pod's verdict.** ``prepare-tenant``'s
    "verify the tenant runs the version under test" resolves the version from
    *LM's catalog record*, which flips as soon as the install lands — while the
    HelmRelease rollout that actually replaces the pod lags behind it. So the
    configmap endpoint can still be the previous image's, serving the previous
    contract, seconds after the version check passes.

    FND-1680's aws leg is that gap, measured: the version check reported
    ``verified: tenant runs sdr-test-634b735e`` at 12:50:02, and six seconds
    later the pod served ``extraction_method`` — the spelling this connector
    renamed to ``extraction-method`` two days earlier. Azure passed the
    identical assertions against the identical build, because its pod had
    already rolled. Nothing distinguishes the two clouds but timing, which is
    the definition of a race.

    Same bound and same cadence as :func:`_await_cards`, because it is the same
    kind of wait: something downstream of the install has not caught up yet. A
    shortfall that survives the whole window is reported as a real finding
    *and says it waited*, so a stale rollout stays distinguishable from a
    contract that genuinely never reached the tenant.
    """
    deadline = time.monotonic() + max(wait_seconds, 0)
    while True:
        failure, form = _check_route(routes, entrypoint, card)
        if failure is None:
            return None, form
        if time.monotonic() >= deadline:
            return (
                f"{failure} Still true after {wait_seconds}s, so this is not "
                "the app's rollout lagging its catalog record."
            ), None
        if on_progress is not None:
            on_progress(
                f"{entrypoint.name}: setup route not ready yet ({failure}); "
                f"retrying in {_CATALOG_POLL_SECONDS}s"
            )
        time.sleep(_CATALOG_POLL_SECONDS)


def verify(
    repo_root: Path,
    routes: RouteReader,
    *,
    generated_dir: str = "app/generated",
    wait_seconds: int = DEFAULT_CATALOG_WAIT_SECONDS,
    on_progress: Callable[[str], None] | None = None,
) -> list[str]:
    """Check every entry point's setup route. Returns the lines to report.

    Raises:
        RouteCheckSkipped: when there is no setup route to check.
        SetupRouteError: when a route is broken or the tenant cannot be asked.
    """
    identity = read_app_identity(repo_root)
    entrypoints = read_entrypoints(repo_root, generated_dir)
    cards = _await_cards(routes, identity, entrypoints, wait_seconds, on_progress)

    # Negative control FIRST. Everything below asserts that a name the tenant
    # knows answers 200; if the endpoint answered 200 for names it does not
    # know, all of it would be vacuous — so prove the endpoint discriminates
    # before trusting a single 200 from it.
    bogus = f"{identity.name}-nonexistent-setup-route-check"
    status, _ = routes.configmap(bogus)
    if status not in _REJECTION_STATUSES:
        raise SetupRouteError(
            f"GET {CONFIGMAP_PATH.format(name=bogus)} returned {status} for a "
            "name that does not exist. Every assertion in this check relies on "
            "an unknown config name being rejected, so none of them proves "
            "anything until that holds."
        )

    report: list[str] = []
    failures: list[str] = []
    for entrypoint in entrypoints:
        card = cards[entrypoint.name]
        label = entrypoint.name

        failure, form = _await_route(
            routes, entrypoint, card, wait_seconds, on_progress
        )
        if failure is not None or form is None:
            failures.append(failure or f"Entrypoint {label!r}: no form served.")
            continue

        via = (
            ""
            if card.id == entrypoint.config_id
            else (
                f" (card id {card.id!r} differs from the generated config id "
                f"{entrypoint.config_id!r}; this app has one contract, so the "
                "app-id fallback resolved it to that one form)"
            )
        )
        report.append(
            f"{label}: /workflows/setup/{card.id} resolves{via}; all "
            f"{len(entrypoint.declared)} declared inputs are served AND drawn, "
            f"across {len(form.steps)} wizard step(s)"
        )

    if failures:
        raise SetupRouteError("\n".join(failures))
    return report
