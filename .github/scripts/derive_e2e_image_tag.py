#!/usr/bin/env python3
"""Derive the e2e test image tag, so it changes when the image content does.

The bug this exists to make impossible
--------------------------------------
The tag was ``sdr-test-<connector-short-sha>``, derived from ``github.sha``
alone. On a connector's own PR that is correct: the SHA moves whenever the
image content moves.

On an **application-sdk** PR it is not. The SDK dispatches an e2e run into each
connector repo, which builds that connector *at its unchanged HEAD* with the
SDK repinned to the PR's commit. Different SDK commit, different image content —
identical tag. So each SDK push overwrote a mutable tag, published it as the
same version name, and prepare-tenant then read the tenant's installed version,
found the name it expected, and skipped:

    tenant already runs 019e3a59-…-e8eae494483f at sdr-test-5027f138 — nothing to do

Nothing rolled. The tenant kept serving whatever content that tag held when it
last pulled, which differed per tenant purely by install history — so the same
SDK commit passed on one cloud and failed on another with no code difference
between them, and "verify the tenant runs the version under test" could never
catch it, because it compares version *names* and the name was identical by
construction.

That makes an e2e run against an already-installed tenant untrustworthy rather
than merely flaky: a green leg is evidence about whatever image that tenant
happens to hold, not about the commit under test.

What goes into the tag
----------------------
Every input that changes the built image's content, and nothing else:

* the connector commit — its own source;
* ``--sdk-ref`` — the application-sdk revision repinned before the build;
* ``--base-image-ref`` — the runtime base an SDK PR rebuilds and builds FROM.

The last two are folded into one short digest rather than spelled out, because
a ref may be a branch name, a tag or a 40-character SHA, and only some of those
are legal in a Docker tag.

Why a suffix and not a wholesale hash
-------------------------------------
A connector PR passes neither ref, and gets **exactly the tag it gets today** —
no churn for the fleet, no orphaned `sdr-test-*` images, and every existing
reference to the `sdr-test-<sha>` shape keeps holding. The suffix appears only
on the dispatch path, which is the only path that was broken.

The connector's short SHA also stays in front, readable, so a tag still says
which connector commit it came from at a glance — which a single opaque hash of
everything would have thrown away.
"""

from __future__ import annotations

import argparse
import hashlib
import sys

#: Prefix every e2e test image carries. Load-bearing beyond cosmetics: the
#: install workflow's error text tells a reader that "sdr-test-* tags are built
#: by e2e runs", and image pruning keys on it. It does not change.
TAG_PREFIX = "sdr-test-"

#: Characters of the connector commit kept in the tag. Matches what the tag has
#: always carried, so a connector-PR tag is byte-identical to today's.
COMMIT_CHARS = 8

#: Characters of the build-input digest appended on the dispatch path.
#:
#: Eight hex characters is 32 bits. These tags are scoped to one connector's
#: package and live only as long as an e2e run's images, so the population is
#: tens, not millions — a collision needs two *different* (sdk-ref, base-image)
#: pairs against the same connector commit landing on the same 32-bit value,
#: which is not a risk worth trading tag readability for.
DIGEST_CHARS = 8


def build_digest(sdk_ref: str, base_image_ref: str) -> str:
    """Short digest over the build inputs that are not the connector commit.

    Order-fixed and separator-delimited so two different pairs cannot collide by
    concatenation (``("ab", "c")`` and ``("a", "bc")`` must not agree). The
    separator is a newline, which cannot appear in a git ref or an image
    reference.

    Args:
        sdk_ref: The application-sdk revision the image is built against.
        base_image_ref: The runtime base image the connector is built FROM.
    """
    material = f"{sdk_ref}\n{base_image_ref}".encode()
    return hashlib.blake2b(material, digest_size=DIGEST_CHARS).hexdigest()[
        :DIGEST_CHARS
    ]


def derive_tag(commit: str, sdk_ref: str = "", base_image_ref: str = "") -> str:
    """Return the image tag for this build.

    Args:
        commit: The connector commit being built (``github.sha``).
        sdk_ref: application-sdk revision repinned before the build, if any.
        base_image_ref: Runtime base image built FROM, if any.

    Returns:
        ``sdr-test-<commit8>`` when neither ref is supplied — byte-identical to
        what a connector's own PR has always produced — and
        ``sdr-test-<commit8>-<digest8>`` when either is.

    Raises:
        ValueError: when *commit* is blank. A tag derived from an empty commit
            would be ``sdr-test-`` for every connector at once, which is worse
            than failing the build: it would silently reintroduce exactly the
            collision this module exists to prevent.
    """
    short = commit.strip()[:COMMIT_CHARS]
    if not short:
        raise ValueError(
            "cannot derive an image tag: no connector commit was supplied. "
            "Pass --commit ${{ github.sha }}."
        )
    base = f"{TAG_PREFIX}{short}"
    sdk_ref, base_image_ref = sdk_ref.strip(), base_image_ref.strip()
    if not sdk_ref and not base_image_ref:
        return base
    return f"{base}-{build_digest(sdk_ref, base_image_ref)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive the e2e test image tag for this build."
    )
    parser.add_argument("--commit", required=True, help="github.sha of the connector")
    parser.add_argument(
        "--sdk-ref", default="", help="application-sdk ref repinned before the build"
    )
    parser.add_argument(
        "--base-image-ref", default="", help="runtime base image built FROM"
    )
    args = parser.parse_args(argv)
    try:
        print(derive_tag(args.commit, args.sdk_ref, args.base_image_ref))
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
