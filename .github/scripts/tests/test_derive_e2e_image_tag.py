"""Does the e2e image tag actually change when the image content changes?

The invariant here is the whole point of the script, and it is not a detail: an
e2e run whose tag does not move republishes the same version name over changed
content, prepare-tenant reads that name off the tenant and skips the install,
and the leg then tests whatever image the tenant already held. That produces a
GREEN leg that is evidence about nothing — the failure mode this suite exists to
make impossible.

So the tests are written as the two halves of that invariant: different content
must never share a tag, and a connector's own PR must keep the tag it has today
so the fix costs the fleet nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from derive_e2e_image_tag import (  # noqa: E402
    TAG_PREFIX,
    build_digest,
    derive_tag,
    main,
)

_CONNECTOR = "5027f138ab8d4c1e9f0a7b6c5d4e3f2a1b0c9d8e"
_OTHER_CONNECTOR = "634b735e11223344556677889900aabbccddeeff"


class TestTheBrokenCase:
    """FND-1680: two SDK commits against one connector must not share a tag."""

    def test_a_new_sdk_ref_moves_the_tag(self) -> None:
        """The exact shape that made three clouds disagree.

        Same connector at the same commit, two different SDK revisions. Before
        this script both produced `sdr-test-5027f138`, so the second push
        overwrote the first's image, republished the same version name, and
        every tenant already holding that name skipped the install.
        """
        first = derive_tag(_CONNECTOR, sdk_ref="ce8f374f8ded4cd5e224cc686539ec4a")
        second = derive_tag(_CONNECTOR, sdk_ref="3fb6cfb4e0000000000000000000000")

        assert first != second

    def test_a_new_base_image_moves_the_tag(self) -> None:
        """An SDK PR rebuilds the runtime base, and the connector is built FROM it.

        The base image reference carries the SDK PR's own SHA
        (`app-runtime-base:pr-3668-sha-ce8f374`), so a base rebuild is a content
        change even when the sdk-ref argument happens to be unchanged.
        """
        first = derive_tag(
            _CONNECTOR, base_image_ref="registry.example.invalid/base:pr-1-sha-aaa"
        )
        second = derive_tag(
            _CONNECTOR, base_image_ref="registry.example.invalid/base:pr-1-sha-bbb"
        )

        assert first != second

    def test_the_connector_commit_still_moves_the_tag(self) -> None:
        """The property that already worked must survive the change."""
        assert derive_tag(_CONNECTOR, sdk_ref="x") != derive_tag(
            _OTHER_CONNECTOR, sdk_ref="x"
        )

    def test_identical_inputs_are_still_deterministic(self) -> None:
        """Every cloud leg and both architecture legs must agree on the tag.

        The build is hoisted ahead of the matrix and each leg reuses the
        reference, and the multi-arch merge recreates it from two per-arch legs.
        A tag that varied per call would break all of that — so this must be a
        pure function of its inputs, with no time, no run id and no randomness.
        """
        args = (_CONNECTOR, "sdk-ref", "base:ref")

        assert derive_tag(*args) == derive_tag(*args)


class TestTheFleetPaysNothing:
    """A connector's own PR must keep the tag it produces today."""

    def test_no_refs_yields_todays_exact_tag(self) -> None:
        """Byte-identical to `sdr-test-$(cut -c1-8)`, which is what shipped."""
        assert derive_tag(_CONNECTOR) == "sdr-test-5027f138"

    def test_blank_refs_are_the_same_as_absent(self) -> None:
        """The action passes empty strings, not unset — they must not add a suffix."""
        assert derive_tag(_CONNECTOR, sdk_ref="", base_image_ref="") == derive_tag(
            _CONNECTOR
        )

    def test_whitespace_only_refs_are_the_same_as_absent(self) -> None:
        """A YAML expression that resolves to nothing can arrive as spaces."""
        assert derive_tag(_CONNECTOR, sdk_ref="  ", base_image_ref="\n") == derive_tag(
            _CONNECTOR
        )

    def test_the_prefix_is_preserved(self) -> None:
        """Pruning and the install workflow's error text both key on it."""
        assert derive_tag(_CONNECTOR, sdk_ref="x").startswith(TAG_PREFIX)

    def test_the_connector_sha_stays_readable_at_the_front(self) -> None:
        """A tag must still say which connector commit it came from."""
        assert derive_tag(_CONNECTOR, sdk_ref="x").startswith("sdr-test-5027f138-")


class TestTagValidity:
    """Whatever the refs look like, the result has to be a legal Docker tag."""

    @pytest.mark.parametrize(
        "sdk_ref",
        (
            "chrishehim/fnd-1680",
            "refs/heads/feature/some_branch",
            "v3.32.1",
            "ce8f374f8ded4cd5e224cc686539ec4afbdaae71",
            "a ref with spaces",
        ),
    )
    def test_a_ref_of_any_shape_yields_a_legal_tag(self, sdk_ref: str) -> None:
        """Branch names carry `/`, which is illegal in a tag — hence the digest."""
        tag = derive_tag(_CONNECTOR, sdk_ref=sdk_ref)

        assert len(tag) <= 128
        assert all(c.isalnum() or c in "._-" for c in tag), tag
        assert tag[0].isalnum()

    def test_the_digest_is_hex_of_the_declared_length(self) -> None:
        digest = build_digest("sdk", "base")

        assert len(digest) == 8
        assert all(c in "0123456789abcdef" for c in digest)

    def test_the_two_inputs_cannot_collide_by_concatenation(self) -> None:
        """Delimited, so ("ab", "c") and ("a", "bc") stay distinct."""
        assert build_digest("ab", "c") != build_digest("a", "bc")


class TestCli:
    def test_it_prints_the_tag(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--commit", _CONNECTOR]) == 0

        assert capsys.readouterr().out.strip() == "sdr-test-5027f138"

    def test_it_accepts_the_refs(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert (
            main(["--commit", _CONNECTOR, "--sdk-ref", "abc", "--base-image-ref", "d"])
            == 0
        )

        assert capsys.readouterr().out.strip() == derive_tag(
            _CONNECTOR, sdk_ref="abc", base_image_ref="d"
        )

    def test_an_empty_commit_fails_loudly(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Never a bare `sdr-test-`, which every connector would then share.

        Failing the build is strictly better than emitting a tag that silently
        reintroduces the collision this whole script exists to prevent.
        """
        assert main(["--commit", "   "]) == 1

        assert "no connector commit" in capsys.readouterr().err
