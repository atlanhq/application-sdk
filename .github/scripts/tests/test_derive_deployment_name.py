"""Tests for .github/actions/sdr-e2e/derive_deployment_name.py.

Mirrors test_build_compose_chain.py: the module under test lives under
.github/actions/sdr-e2e/ (invoked by the composite action), the test lives here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "actions" / "sdr-e2e"))

from derive_deployment_name import derive, main  # noqa: E402


def test_no_suffix_keeps_single_suite_shape() -> None:
    assert derive("12345", "") == "e2e-full-ci-12345"


def test_suffix_is_appended() -> None:
    assert derive("12345", "connection-create") == "e2e-full-ci-12345-connection-create"


def test_distinct_suffixes_yield_distinct_names() -> None:
    a = derive("12345", "connection-create")
    b = derive("12345", "connection-reuse")
    assert a != b


def test_empty_run_id_falls_back_to_local() -> None:
    assert derive("", "connection-reuse") == "e2e-full-ci-local-connection-reuse"


def test_suffix_is_sanitised_and_lowercased() -> None:
    # Whitespace / mixed case / stray punctuation collapse to hyphen-joined lower.
    assert derive("7", " Connection Create! ") == "e2e-full-ci-7-connection-create"


def test_suffix_of_only_punctuation_is_dropped() -> None:
    assert derive("7", "///") == "e2e-full-ci-7"


def test_run_id_is_stripped() -> None:
    assert derive("  99  ", "") == "e2e-full-ci-99"


def test_main_prints_github_env_line(capsys: pytest.CaptureFixture[str]) -> None:
    sys.argv = ["derive_deployment_name.py", "--run-id", "42", "--suffix", "conn-x"]
    main()
    out = capsys.readouterr().out.strip()
    assert out == "ATLAN_DEPLOYMENT_NAME=e2e-full-ci-42-conn-x"
