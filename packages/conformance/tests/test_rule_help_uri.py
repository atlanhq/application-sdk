"""Every rule's SARIF ``helpUri`` resolves to a page that documents that rule.

46 rules once linked to a repo-root ``conformance/docs/rules/`` path removed
when the package moved under ``packages/``, and every one of their alerts in
a fleet repo's code-scanning view led to a 404.  Nothing checked the path, only
the anchor suffix.  ``help_uri`` is now derived from the rule id; these tests
pin the derivation to files that exist in this package.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import conformance
from conformance import __version__
from conformance.suite.rules import load_catalog
from conformance.suite.schema.catalog import (
    RULE_DOCS_BLOB_BASE,
    RuleDefinition,
    rule_help_uri,
)

_PACKAGE_ROOT = Path(conformance.__file__).resolve().parent
_REPO_PREFIX = "https://github.com/atlanhq/application-sdk/blob/"


def _local_path(uri: str) -> Path:
    """Map a help URI onto this checkout: strip the repo and ref, keep the path."""
    assert uri.startswith(_REPO_PREFIX), uri
    _ref, _, repo_path = uri.removeprefix(_REPO_PREFIX).partition("/")
    package_relative = repo_path.removeprefix("packages/conformance/conformance/")
    assert package_relative != repo_path, f"{uri} is outside the conformance package"
    return _PACKAGE_ROOT / package_relative


def test_links_are_pinned_to_the_installed_release_tag() -> None:
    assert RULE_DOCS_BLOB_BASE.startswith(f"{_REPO_PREFIX}conformance-v{__version__}/")


@pytest.mark.parametrize("rule", load_catalog(), ids=lambda rule: rule.id)
def test_help_uri_resolves_to_the_rules_own_page(rule: RuleDefinition) -> None:
    descriptor_uri = rule.to_reporting_descriptor().help_uri
    assert descriptor_uri == rule.help_uri == rule_help_uri(rule.id)
    assert "#" not in descriptor_uri, "GitHub does not render {#id} heading anchors"
    page = _local_path(descriptor_uri)
    assert page.is_file(), f"{rule.id}: {descriptor_uri} has no file at {page}"
    first_heading = next(
        line
        for line in page.read_text(encoding="utf-8").splitlines()
        if line.startswith("# ")
    )
    assert first_heading.startswith(f"# {rule.id} "), first_heading


def test_a_hand_written_help_uri_is_rejected() -> None:
    with pytest.raises(ValidationError, match="help_uri is derived from the rule id"):
        RuleDefinition.model_validate(
            {
                "id": "Z999",
                "name": "Example",
                "tier": "warn",
                "scope": "app",
                "category": "example",
                "help_uri": "https://example.invalid/rules.md#z999",
            }
        )
