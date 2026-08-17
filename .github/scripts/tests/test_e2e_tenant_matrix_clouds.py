"""Tests for .github/scripts/e2e_tenant_matrix_clouds.py (FND-354).

The script is the only thing that reads ``E2E_TENANT_MATRIX_JSON`` outside the
per-leg resolver, so two properties matter more than the parsing: it emits KEYS
and never values, and an unusable payload degrades to "not known" rather than
failing the run out from under the resolver's precise per-leg diagnosis.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from e2e_tenant_matrix_clouds import MatrixCloudsError, cloud_keys, main  # noqa: E402

_SECRET = "s3cr3t-client-secret"
_MATRIX = {
    "aws": {
        "tenant": "tenant-a.example.com",
        "client_id": "id-a",
        "client_secret": _SECRET,
        "api_key": "key-a",
    },
    "gcp": {
        "tenant": "tenant-b.example.com",
        "client_id": "id-b",
        "client_secret": _SECRET,
        "api_key": "key-b",
    },
}


def _clouds(out: str) -> str:
    line = next(ln for ln in out.splitlines() if ln.startswith("clouds="))
    return line[len("clouds=") :]


def test_returns_the_cloud_keys_sorted() -> None:
    assert cloud_keys(json.dumps({"gcp": {}, "aws": {}, "azure": {}})) == [
        "aws",
        "azure",
        "gcp",
    ]


def test_empty_payload_yields_no_keys() -> None:
    # The secret is not shared with this repo; the caller already collapses the
    # cloud dimension to "none" in that case.
    assert cloud_keys("") == []
    assert cloud_keys("   ") == []


def test_a_key_counts_as_available_even_with_a_broken_entry() -> None:
    # Presence, not validity. A cloud whose entry is missing client_secret is a
    # coverage hole that must stay red — the per-leg tenant resolver names the
    # missing field per leg. Narrowing it away here would turn that red into a
    # run that looks complete and is not.
    assert cloud_keys(json.dumps({"aws": {}, "gcp": None})) == ["aws", "gcp"]


@pytest.mark.parametrize("payload", ["{not json", '["aws"]', '"aws"', "42"])
def test_unusable_payloads_raise(payload: str) -> None:
    with pytest.raises(MatrixCloudsError):
        cloud_keys(payload)


def test_main_prints_the_keys_as_a_github_output(capsys) -> None:
    rc = main(["--matrix-json", json.dumps(_MATRIX)])
    out = capsys.readouterr().out
    assert rc == 0
    assert _clouds(out) == "aws,gcp"


def test_main_never_prints_a_credential(capsys) -> None:
    main(["--matrix-json", json.dumps(_MATRIX)])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for value in ("id-a", "key-a", "tenant-a.example.com", _SECRET):
        assert value not in combined, (
            "only cloud KEYS may cross this boundary — stdout here is redirected "
            "into $GITHUB_OUTPUT and stderr is the run log, and neither is masked "
            "for a value extracted out of a registered blob"
        )


def test_main_degrades_to_no_keys_and_warns_on_a_bad_payload(capsys) -> None:
    # Exit 0 with an empty list, deliberately: empty means "not known",
    # discovery then narrows nothing, and the run behaves exactly as it did
    # before FND-354 — including the per-leg resolver error, which is the one
    # that can name the actual defect in the secret.
    rc = main(["--matrix-json", '{"aws": '])
    captured = capsys.readouterr()
    assert rc == 0
    assert _clouds(captured.out) == ""
    assert "::warning::" in captured.err


def test_main_does_not_echo_the_payload_when_it_cannot_be_parsed(capsys) -> None:
    # A malformed blob is still a blob full of credentials. The decode error's
    # position is quotable; its content is not.
    main(["--matrix-json", '{"aws": {"client_secret": "' + _SECRET + '"'])
    captured = capsys.readouterr()
    assert _SECRET not in captured.out + captured.err
