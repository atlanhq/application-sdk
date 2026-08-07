"""Tests for .github/scripts/assert_image_platforms.py.

The failure this guards is not a build error — it is an image that builds,
publishes and installs cleanly and then cannot be pulled by the tenant's node.
So the cases that matter are the ones where something *looks* fine: a
single-platform manifest where an index was expected, and an index padded out by
buildx's attestation entries.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import assert_image_platforms as ap  # noqa: E402

_BOTH = "linux/amd64,linux/arm64"


def _index(*platforms: dict[str, str]) -> str:
    """An OCI index whose entries carry the given platform objects."""
    return json.dumps(
        {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"digest": f"sha256:{i}", "platform": platform}
                for i, platform in enumerate(platforms)
            ],
        }
    )


_AMD64 = {"os": "linux", "architecture": "amd64"}
_ARM64 = {"os": "linux", "architecture": "arm64", "variant": "v8"}
#: What buildx attaches for provenance/SBOM. Not a platform.
_ATTESTATION = {"os": "unknown", "architecture": "unknown"}


# ── The happy path ───────────────────────────────────────────────────────────


def test_a_two_arch_index_passes() -> None:
    assert ap.assert_platforms(_index(_AMD64, _ARM64), _BOTH) == [
        "linux/amd64",
        "linux/arm64",
    ]


def test_the_arm64_variant_does_not_have_to_be_spelled_out() -> None:
    """buildx normalises linux/arm64 to variant v8 on push.

    Comparing the raw strings would make the check fail on a correct image, which
    is worse than not having it: it would train the next person to delete it.
    """
    assert ap.assert_platforms(_index(_AMD64, _ARM64), "linux/arm64") == [
        "linux/amd64",
        "linux/arm64",
    ]


def test_extra_platforms_are_not_an_error() -> None:
    """The contract is "serves at least these", so adding linux/s390x is fine."""
    index = _index(_AMD64, _ARM64, {"os": "linux", "architecture": "s390x"})
    ap.assert_platforms(index, _BOTH)


def test_attestation_entries_are_not_counted_as_platforms() -> None:
    index = _index(_AMD64, _ATTESTATION, _ATTESTATION)
    with pytest.raises(ap.PlatformAssertError, match="linux/arm64"):
        ap.assert_platforms(index, _BOTH)
    assert ap.platforms_in(index) == ["linux/amd64"]


def test_duplicate_entries_are_reported_once() -> None:
    assert ap.platforms_in(_index(_AMD64, _AMD64)) == ["linux/amd64"]


# ── The failures worth catching ───────────────────────────────────────────────


def test_a_missing_arch_names_it_and_says_what_breaks() -> None:
    with pytest.raises(ap.PlatformAssertError) as excinfo:
        ap.assert_platforms(_index(_AMD64), _BOTH)
    message = str(excinfo.value)
    assert "linux/arm64" in message
    # The point of the message: nothing between here and the tenant's kubelet
    # rejects this image, so the reader has to be told where it will surface.
    assert "ImagePullBackOff" in message or "no matching manifest" in message


def test_a_single_platform_manifest_is_a_failure_not_a_pass() -> None:
    """A dropped --platform yields an image manifest, not an index.

    `manifests` is absent, so a check that only iterated entries would find
    nothing missing and pass — the exact silent-success this script exists to
    prevent.
    """
    raw = json.dumps(
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:cfg"},
            "layers": [],
        }
    )
    with pytest.raises(ap.PlatformAssertError, match="single-platform"):
        ap.assert_platforms(raw, _BOTH)


def test_an_index_of_only_attestations_is_a_failure() -> None:
    with pytest.raises(ap.PlatformAssertError, match="single-platform"):
        ap.assert_platforms(_index(_ATTESTATION), _BOTH)


def test_an_empty_expectation_is_refused() -> None:
    # Reading as "nothing required, therefore satisfied" would disable the guard
    # from a typo in the workflow.
    for empty in ("", "   ", ",,"):
        with pytest.raises(ap.PlatformAssertError, match="no expected platforms"):
            ap.assert_platforms(_index(_AMD64), empty)


@pytest.mark.parametrize("bad", ["amd64", "linux", "/", "linux/"])
def test_a_malformed_expectation_is_refused(bad: str) -> None:
    with pytest.raises(ap.PlatformAssertError, match="not a platform"):
        ap.assert_platforms(_index(_AMD64), bad)


def test_non_json_input_names_the_command_that_produces_it() -> None:
    with pytest.raises(ap.PlatformAssertError, match="imagetools inspect"):
        ap.assert_platforms("unauthorized: authentication required", _BOTH)


def test_a_json_array_is_refused() -> None:
    with pytest.raises(ap.PlatformAssertError, match="JSON object"):
        ap.assert_platforms("[]", _BOTH)


@pytest.mark.parametrize(
    "entry",
    [
        {"os": "linux"},  # no architecture
        {"architecture": "amd64"},  # no os
        {},
        {"os": "", "architecture": ""},
    ],
)
def test_an_incomplete_platform_object_is_ignored(entry: dict[str, str]) -> None:
    # Ignored rather than trusted: a half-filled platform must not satisfy an
    # expectation it cannot be compared against.
    assert ap.platforms_in(_index(entry)) == []


def test_a_manifests_key_of_the_wrong_type_does_not_crash() -> None:
    assert ap.platforms_in(json.dumps({"manifests": "nope"})) == []


def test_a_non_object_entry_is_skipped() -> None:
    assert ap.platforms_in(
        json.dumps({"manifests": ["nope", {"platform": _AMD64}]})
    ) == ["linux/amd64"]


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_main_reads_a_file_and_reports_what_it_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "raw.json"
    manifest.write_text(_index(_AMD64, _ARM64), encoding="utf-8")
    code = ap.main(["--expected", _BOTH, "--manifest-file", str(manifest)])
    assert code == 0
    assert "linux/amd64, linux/arm64" in capsys.readouterr().out


def test_main_reads_stdin_when_no_file_is_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The pipeline form the action uses: `imagetools inspect --raw | this`.
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(_index(_AMD64, _ARM64)))
    assert ap.main(["--expected", _BOTH]) == 0


def test_main_emits_a_workflow_error_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "raw.json"
    manifest.write_text(_index(_AMD64), encoding="utf-8")
    code = ap.main(["--expected", _BOTH, "--manifest-file", str(manifest)])
    assert code == 1
    # ::error:: so the annotation lands on the step rather than only in the log.
    assert capsys.readouterr().err.startswith("::error::")
