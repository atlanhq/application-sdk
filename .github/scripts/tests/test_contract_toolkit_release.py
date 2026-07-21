"""Tests for .github/scripts/contract_toolkit_release.py.

Focus: ``regenerate_toolkit_baseline`` — the release step that keeps the
committed conformance baseline in lock-step with the toolkit version bump so
the release PR clears its own K007/K008 drift gate
(``test_toolkit_baseline_drift``). The output format here MUST match
``conformance.suite.checks._toolkit_baseline.serialize`` exactly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import contract_toolkit_release as mod

_PKLPROJECT = """\
amends "pkl:Project"

package {
  name = "app-contract-toolkit"
  baseUri = "package://atlanhq.github.io/application-sdk/contracts/\\(name)"
  version = "1.2.3"
  packageZipUrl = "https://atlanhq.github.io/application-sdk/contracts/\\(name)@\\(version).zip"
}
"""


def _write_pklproject(tmp_path: Path, text: str = _PKLPROJECT) -> Path:
    p = tmp_path / "PklProject"
    p.write_text(text, encoding="utf-8")
    return p


def test_regenerate_baseline_interpolates_name_and_strips_scheme(
    tmp_path, monkeypatch
) -> None:
    pkl = _write_pklproject(tmp_path)
    out = tmp_path / "toolkit_baseline.json"
    monkeypatch.setattr(mod, "PKLPROJECT", str(pkl))
    monkeypatch.setattr(mod, "TOOLKIT_BASELINE", str(out))

    mod.regenerate_toolkit_baseline()

    content = out.read_text(encoding="utf-8")
    # \(name) resolved, package:// scheme stripped, version carried through.
    assert json.loads(content) == {
        "canonical_base": "atlanhq.github.io/application-sdk/contracts/app-contract-toolkit",
        "latest_version": "1.2.3",
    }


def test_regenerate_baseline_matches_conformance_serialize_format(
    tmp_path, monkeypatch
) -> None:
    """Byte-for-byte match with the conformance serializer: sorted keys,
    two-space indent, single trailing newline. If either side changes its
    format, the drift test would flake — this pins them together."""
    pkl = _write_pklproject(tmp_path)
    out = tmp_path / "toolkit_baseline.json"
    monkeypatch.setattr(mod, "PKLPROJECT", str(pkl))
    monkeypatch.setattr(mod, "TOOLKIT_BASELINE", str(out))

    mod.regenerate_toolkit_baseline()

    expected = (
        json.dumps(
            {
                "canonical_base": "atlanhq.github.io/application-sdk/contracts/app-contract-toolkit",
                "latest_version": "1.2.3",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    assert out.read_text(encoding="utf-8") == expected


def test_regenerate_baseline_exits_on_unparseable_pklproject(
    tmp_path, monkeypatch
) -> None:
    pkl = _write_pklproject(tmp_path, text='package {\n  name = "x"\n}\n')
    out = tmp_path / "toolkit_baseline.json"
    monkeypatch.setattr(mod, "PKLPROJECT", str(pkl))
    monkeypatch.setattr(mod, "TOOLKIT_BASELINE", str(out))

    with pytest.raises(SystemExit):
        mod.regenerate_toolkit_baseline()
    assert not out.exists()
