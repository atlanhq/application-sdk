"""The build identity of the image this process is running from (FND-1684).

Why this exists
---------------
Nothing an app could already report distinguishes *the image built by this CI
run* from an image built months ago:

* ``App._app_version`` / ``AppContext.app_version`` is a semver declared in the
  app's own source (``@App(version=...)``). It is identical across every build
  of that source, so it cannot tell one build from another.
* The served manifest is ``{dag, execution_mode}`` — no version at all.
* ``/api/service/configmaps/{name}`` serves committed files verbatim, and the
  identity CI compares against (the image tag) is minted *after* every committed
  artifact exists, so no contract or pkl-generated file can carry it.

That gap is what let the e2e version check read its own input: ``install``
skipped when the marketplace install record already named the expected version,
and ``verify`` then read the same record. Once that record existed for a build,
the check could only agree with it — it established nothing about what the
cluster was actually serving, which is the one thing it exists to establish.

So the build identity is stamped into the image at build time and read back out
here. It is the one fact only a *running pod* can report, which is what makes it
worth reading.

The contract
------------
``ATLAN_BUILD_ID`` is set as an image ``ENV`` by whatever builds the image. In
Atlan CI that is ``.github/actions/build-app-image`` in ``application-sdk``,
which passes the image tag it just derived (``sdr-test-<commit8>[-<digest8>]``)
as a ``--build-arg`` and stamps the connector Dockerfile to promote it to an
``ENV`` — see ``.github/scripts/stamp_build_identity.py``.

That action builds only the *e2e* image. A **released** image is built by
``.github/workflows/build-and-publish-app.yaml``, which never calls it, so
production images carried no identity at all. They now carry the same value in
the ``build_id`` key of the ``app/atlan_build.json`` the publish workflow bakes
into the build context, and this function falls back to it.

One identity, two carriers, in this order:

1. the ``ATLAN_BUILD_ID`` ENV — the e2e path, unchanged, and still first so a
   stamped image reports exactly what it reports today;
2. ``app/atlan_build.json``'s ``build_id`` — the publish path.

Both hold the same *kind* of value: the immutable, arch-independent image tag,
minted after every committed artifact exists and therefore unforgeable by
anything in the app's source. The alternative — a second env var, a second
reader, and a second thing an e2e check has to know how to ask for — would make
"which build is this?" a question with two answers that can disagree.

It is deliberately **not required**: an image built by hand, by a connector's own
Dockerfile, or by an older CI simply reports ``""``. Every reader treats an empty
value as "this image carries no build identity", never as a mismatch.

Reading it
----------
Registration copies it onto the App class (``App._app_build_id``) and the
execution layer copies it onto :class:`~application_sdk.app.context.AppContext`,
so every app inherits it with no per-app change. The handler serves it on the
already-proxied configmap route under :data:`BUILD_IDENTITY_CONFIGMAP_ID`, so
reading it from outside the cluster needs no new Heracles rule.
"""

from __future__ import annotations

import os

from application_sdk.constants import load_build_info

__all__ = [
    "BUILD_ID_ENV",
    "BUILD_IDENTITY_CONFIGMAP_ID",
    "BUILD_INFO_BUILD_ID_KEY",
    "build_identity",
]

#: Image ``ENV`` carrying the build identity. Stamped at image build time.
BUILD_ID_ENV = "ATLAN_BUILD_ID"

#: The configmap id the handler answers with the build identity.
#:
#: Reserved rather than derived: it must not collide with any generated
#: ``*.json`` stem, and it must be stable across apps so one CI check can ask
#: every app the same question. The ``atlan-`` prefix matches the marketplace's
#: own configmap naming (``atlan-connectors-<source>``), and the handler answers
#: it *before* the generated-file scan so an app that happens to ship a file of
#: this name cannot shadow it.
BUILD_IDENTITY_CONFIGMAP_ID = "atlan-build-identity"


#: Key carrying the build identity in the baked ``app/atlan_build.json``.
#:
#: Must match what ``build-and-publish-app.yaml``'s "Bake build identity into the
#: image" step writes; the wiring test asserts the two agree, for the same reason
#: :data:`BUILD_ID_ENV` has one — a divergent spelling degrades the check to "the
#: pod reports no build identity" rather than failing loudly.
BUILD_INFO_BUILD_ID_KEY = "build_id"


def build_identity() -> str:
    """Return this image's build identity, or ``""`` when it carries none.

    Reads both carriers on every call rather than caching at import. In
    production neither can change — an image ``ENV`` and a file inside the image
    are both fixed for the life of the container — but tests and local runs set
    them per case, and a module-level snapshot would freeze whichever value
    happened to exist at first import.

    The file read costs one ``open`` of a five-key JSON, against call sites (an
    activity's identity field, an HTTP route) that are each orders of magnitude
    larger. :data:`application_sdk.constants.APPLICATION_VERSION` snapshots the
    same file at import instead, which is correct there — it is a module
    constant — and indistinguishable in production, where the file cannot
    change under either of them.
    """
    stamped = os.environ.get(BUILD_ID_ENV, "").strip()
    if stamped:
        return stamped
    return load_build_info().get(BUILD_INFO_BUILD_ID_KEY, "").strip()
