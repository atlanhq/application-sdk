"""The shape of the committed ``app/generated/`` tree, in one place.

Three facts about that tree were, until FND-1667, each written down twice:

* **which layout the tree has** — a bundle nests one directory per entry point,
  while a single-generated-contract app (including a route/card-split app with
  several ``@entrypoint``\\ s behind one card) emits everything flat.
  :func:`generated_layout` is the authority, and
  :mod:`application_sdk.app._artifact_schema_guard` delegates to it.
* **which of the sibling ``*.json`` files is a setup form** — as opposed to the
  DAG ``manifest`` or a per-object-store-family credential template.
  :func:`is_form_configmap` is the authority, and
  :mod:`application_sdk.handler.service` delegates to it.
* **where an entry point's form actually lives**, given the two above.
  :func:`form_configmap` is the authority; the tenant-side route check in
  :mod:`application_sdk.testing.setup_routes` is its first caller.

Keeping them here is not tidying. The route check added in FND-1667 asserts a
join between what an app's contract generates and what its tenant serves, and
the SDK is on *both* sides of that join: ``handler.service.get_configmap`` is
the endpoint the tenant's ``/api/service/configmaps/<name>`` proxies to. A
check that re-derived "which file is the form" would be comparing one guess
against another, and would drift from the server the moment the exclusion
vocabulary grew a prefix. There is one copy of each fact, and the check reads
the same copy the server does.

Deliberately dependency-light: stdlib only, no logger, no constants import. It
is imported by the request path in ``handler.service`` and by a CI-time check,
so it must cost nothing and pull nothing.

Private (leading underscore), like its siblings ``_artifact_schema_guard`` and
``_ep_registration``: these are the SDK's own rules about its own generated
tree, not a surface a consumer app should pin against. Nothing here is
re-exported from ``application_sdk.app``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

__all__ = [
    "ARTIFACT_SCHEMAS_STEM",
    "CREDENTIAL_TEMPLATE_PREFIXES",
    "GeneratedLayout",
    "MANIFEST_STEM",
    "NON_FORM_STEMS",
    "choose_form_configmap",
    "eligible_form_configmaps",
    "form_configmap",
    "generated_layout",
    "is_form_configmap",
    "names_entrypoint",
    "pick_form_configmap",
]

#: Stem of the DAG manifest that sits alongside the generated setup forms.
MANIFEST_STEM = "manifest"

#: Stem of the artifact-schema declarations the toolkit emits for an app with an
#: ``artifactSchemas`` block (conformance K016). Read by
#: :mod:`application_sdk.validation.sources` and
#: :mod:`application_sdk.app._artifact_schema_guard`; named here because it is
#: also a *non-form* sibling and form discovery must skip it.
ARTIFACT_SCHEMAS_STEM = "artifact_schemas"

#: Whole stems — as opposed to the family prefixes below — that are never a
#: setup form.
#:
#: ``artifact_schemas`` earns its place the hard way (FND-1682): it sorts before
#: every real form stem, so once an app declared ``artifactSchemas`` the
#: alphabetical fallback in ``handler.service.get_configmap`` served the schema
#: document as the form. It has no ``config`` key and no ``properties``, so the
#: setup wizard rendered blank behind an HTTP 200 — nothing in the logs, the
#: network tab or pod stderr.
NON_FORM_STEMS = frozenset({MANIFEST_STEM, ARTIFACT_SCHEMAS_STEM})

#: Non-form JSON siblings that live in the generated dir next to the setup-form
#: configmaps. Credential templates are emitted per object-store family
#: (``atlan-connectors-*.json``, ``csa-connectors-*.json``).
#:
#: Centralised so the form-discovery exclusion vocabulary is named once instead
#: of re-spelled at each site: adding the next connector-family prefix here
#: updates the server's fallback (``handler.service.get_configmap``) and the
#: tenant-side route check together. They disagreeing is the failure this
#: constant exists to make impossible — the server would serve one file and the
#: check would compare against another, and the mismatch would read as a
#: contract regression rather than as two different exclusion lists.
CREDENTIAL_TEMPLATE_PREFIXES = ("atlan-connectors-", "csa-connectors-")

#: Shape of the committed ``app/generated/`` tree — the authority on where an
#: entry point's generated artifacts live. Mirrors conformance K016's contract
#: scan.
GeneratedLayout = Literal["multi", "single", "unknown"]


def is_form_configmap(stem: str) -> bool:
    """Whether a generated JSON stem is a setup-form configmap.

    True when *stem* is neither one of the well-known non-form documents
    (:data:`NON_FORM_STEMS`) nor a credential template.

    **This list can only ever name the siblings the toolkit emits today.**
    FND-1682 is what a stale one costs: ``artifact_schemas.json`` was not on it,
    sorted before every real form stem, and so was served as the form by every
    app that adopted conformance K016. A caller with an entry-point name to go
    on should ask :func:`pick_form_configmap`, which prefers a form that
    :func:`names_entrypoint` over anything this filter merely fails to reject.

    Args:
        stem: A generated file's name without its ``.json`` suffix.
    """
    return stem not in NON_FORM_STEMS and not stem.startswith(
        CREDENTIAL_TEMPLATE_PREFIXES
    )


def names_entrypoint(stem: str, entrypoint: str) -> bool:
    """Whether a form's *stem* identifies itself as *entrypoint*'s.

    Two spellings are in the field, and both are identifications rather than
    guesses:

    * ``<entrypoint>`` — a flat app whose form is named for its entry point
      (``bridge.json`` for ``bridge``).
    * ``<source>-<entrypoint>`` — the connector convention, where the entry
      point is the *role* and the file carries the source (``crawler`` →
      ``snowflake-crawler.json``, ``miner`` → ``postgres-miner.json``).

    The suffix spelling is not a nicety: across the connector fleet the exact
    spelling matches almost nothing, because the entry points are called
    ``crawler`` and ``miner`` while the files are named for the source. Matching
    only the exact form leaves every connector on the last-resort scan.

    Args:
        stem: A generated file's name without its ``.json`` suffix.
        entrypoint: The entry point's kebab-case wire name.
    """
    return stem == entrypoint or stem.endswith(f"-{entrypoint}")


def eligible_form_configmaps(directory: Path) -> list[Path]:
    """Every ``*.json`` in *directory* that could be a setup form, sorted.

    The non-form siblings (:func:`is_form_configmap`) are already gone. What
    remains is what any pick has to choose between, and a caller that wants to
    *report* on the choice — the configmap endpoint warns when it had to fall
    back to alphabetical order among several — needs the list, not just the
    winner.

    Args:
        directory: A directory in the generated tree. Missing or not a
            directory yields ``[]``.
    """
    if not directory.is_dir():
        return []
    return [
        candidate
        for candidate in sorted(directory.glob("*.json"))
        if is_form_configmap(candidate.stem)
    ]


def choose_form_configmap(candidates: Sequence[Path], entrypoint: str) -> Path | None:
    """Pick *entrypoint*'s form out of *candidates*, or ``None`` if empty.

    Three steps, in this order:

    1. The candidate whose stem :func:`names_entrypoint` — an identification.
    2. The only candidate, when there is exactly one. Nothing else it could be.
    3. Otherwise the alphabetically first, which **is** a guess.

    **Step 3 stays, deliberately.** It is the compatibility path for every app
    that has not adopted a name the SDK can recognise, and dropping it (or
    turning ambiguity into a 404) would break apps that work today in order to
    protect against a sibling the toolkit does not yet emit. FND-1682 was not
    caused by the guess being *reachable*; it was caused by
    ``artifact_schemas.json`` being eligible at all, which
    :data:`NON_FORM_STEMS` now fixes. Steps 1 and 2 shrink how often step 3 has
    to run — across the connector fleet they answer every generated directory —
    but the endpoint that calls this logs a warning when step 3 does run with
    more than one candidate, so a future sibling shows up in the logs instead of
    only in a blank wizard.

    Args:
        candidates: Eligible forms, from :func:`eligible_form_configmaps`.
        entrypoint: The entry point's kebab-case wire name.
    """
    if not candidates:
        return None
    named = [c for c in candidates if names_entrypoint(c.stem, entrypoint)]
    if len(named) == 1:
        return named[0]
    return candidates[0]


def pick_form_configmap(directory: Path, entrypoint: str) -> Path | None:
    """Return *directory*'s setup form for *entrypoint*, or ``None``.

    :func:`eligible_form_configmaps` then :func:`choose_form_configmap` — the
    convenience spelling for callers that do not need the candidate list.

    Args:
        directory: A directory in the generated tree. Missing or not a
            directory yields ``None``.
        entrypoint: The entry point's kebab-case wire name.
    """
    return choose_form_configmap(eligible_form_configmaps(directory), entrypoint)


def generated_layout(generated: Path) -> GeneratedLayout:
    """Classify the committed generated tree by the shape it actually has.

    ``multi``
        One or more immediate subdirectories each holding a ``manifest.json``.
        Each subdirectory name is an entry point's wire name.
    ``single``
        A ``manifest.json`` at the root of *generated* and no per-entry-point
        subdirectories.
    ``unknown``
        No generated tree, or one carrying no ``manifest.json`` anywhere (a repo
        that has not generated yet). Nothing can be inferred about the layout,
        and every caller says so rather than guessing.

    **Why the tree and not the Python entry-point count.** The layout is a
    property of what the toolkit emitted, not of how many ``@entrypoint``
    methods Python happens to see. A **route/card-split** app (BLDX-1342) is
    exactly where the two disagree: it has several ``@entrypoint``\\ s the DAG
    invokes by ``workflow_type``, but one marketplace card and therefore one
    *flat* generated tree. Counting Python entry points calls it a bundle and
    sends every consumer looking under a wire-name subdirectory the toolkit
    will never write for that app.

    Args:
        generated: The generated directory, usually ``app/generated``.
    """
    try:
        children = list(generated.iterdir())
    except OSError:
        return "unknown"
    if any((child / "manifest.json").is_file() for child in children if child.is_dir()):
        return "multi"
    if (generated / "manifest.json").is_file():
        return "single"
    return "unknown"


def form_configmap(
    generated: Path, entrypoint: str, *, layout: GeneratedLayout | None = None
) -> Path | None:
    """Return the setup-form configmap for *entrypoint*, or ``None``.

    Which directory is searched follows the *layout*, not the caller's belief
    about how many entry points exist — see :func:`generated_layout`:

    ``multi``
        ``<generated>/<entrypoint>/`` only. A bundle's forms are nested, and
        falling back to the root would let the root-level credential template
        or another entry point's form answer for this one.
    ``single``
        ``<generated>/`` only, and *entrypoint* is ignored: a flat tree has one
        form serving every route behind the single card.
    ``unknown``
        The nested directory, then the flat one — best effort, since a repo
        that has not generated has nothing to be right about.

    Within the chosen directory :func:`pick_form_configmap` decides, so a form
    named after the entry point is identified by name and anything else falls
    back to the sorted :func:`is_form_configmap` scan. That exclusion is what
    keeps a flat app's ``atlan-connectors-<source>.json`` from being mistaken
    for its form: sorted alphabetically the credential template comes *first*,
    so a glob without the exclusion picks the wrong file on every
    single-entrypoint connector.

    Args:
        generated: The generated directory, usually ``app/generated``.
        entrypoint: The entry point's kebab-case wire name, which is also its
            directory name in a bundle's generated tree.
        layout: The tree's shape. Computed from *generated* when omitted.

    Returns:
        The form's path, or ``None`` when the chosen directories hold no file
        that qualifies.
    """
    resolved = generated_layout(generated) if layout is None else layout
    if resolved == "single":
        search: tuple[Path, ...] = (generated,)
    elif resolved == "multi":
        search = (generated / entrypoint,)
    else:
        search = (generated / entrypoint, generated)

    for directory in search:
        found = pick_form_configmap(directory, entrypoint)
        if found is not None:
            return found
    return None
