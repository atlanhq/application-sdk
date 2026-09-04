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

from pathlib import Path
from typing import Literal

__all__ = [
    "CREDENTIAL_TEMPLATE_PREFIXES",
    "GeneratedLayout",
    "MANIFEST_STEM",
    "form_configmap",
    "generated_layout",
    "is_form_configmap",
]

#: Stem of the DAG manifest that sits alongside the generated setup forms.
MANIFEST_STEM = "manifest"

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

    True when *stem* is neither the DAG ``manifest`` nor a credential template.

    Args:
        stem: A generated file's name without its ``.json`` suffix.
    """
    return stem != MANIFEST_STEM and not stem.startswith(CREDENTIAL_TEMPLATE_PREFIXES)


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

    Within the chosen directory the first stem that :func:`is_form_configmap`
    accepts wins, in sorted order for determinism. That exclusion is what keeps
    a flat app's ``atlan-connectors-<source>.json`` from being mistaken for its
    form: sorted alphabetically the credential template comes *first*, so a
    glob without the exclusion picks the wrong file on every single-entrypoint
    connector.

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
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.json")):
            if is_form_configmap(candidate.stem):
                return candidate
    return None
