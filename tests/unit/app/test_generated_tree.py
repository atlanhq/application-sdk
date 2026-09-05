"""FND-1682: which sibling of the generated tree is the setup form.

The generated dir holds an entry point's form next to files that are *not*
forms — the DAG ``manifest``, the per-object-store-family credential templates,
and (since conformance K016) ``artifact_schemas``. Picking the wrong one is
silent by construction: the configmap endpoint returns HTTP 200 carrying a
document with no ``properties``, and the setup wizard renders blank with nothing
in the logs, the network tab or pod stderr.

These tests pin both halves of the answer — the exclusion vocabulary, and the
name-first pick that keeps the *next* new sibling from repeating FND-1682.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from application_sdk.app._generated_tree import (
    ARTIFACT_SCHEMAS_STEM,
    form_configmap,
    generated_layout,
    is_form_configmap,
    pick_form_configmap,
)


def _write(directory: Path, *names: str) -> None:
    """Create *names* under *directory* with throwaway JSON content."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(json.dumps({"stem": Path(name).stem}))


class TestIsFormConfigMap:
    """The exclusion vocabulary — the fallback half of form discovery."""

    @pytest.mark.parametrize(
        "stem",
        [
            "manifest",
            "artifact_schemas",
            "atlan-connectors-snowflake",
            "csa-connectors-objectstore",
        ],
    )
    def test_non_form_siblings_rejected(self, stem: str) -> None:
        assert is_form_configmap(stem) is False

    @pytest.mark.parametrize("stem", ["metabase", "snowflake-crawler", "openapi"])
    def test_form_stems_accepted(self, stem: str) -> None:
        assert is_form_configmap(stem) is True

    def test_artifact_schemas_sorts_before_a_real_form(self) -> None:
        """The property that made FND-1682 silent rather than loud.

        ``artifact_schemas`` sorts before every plausible form stem, so a sorted
        scan without the exclusion picks it *first* on any app that adopted
        K016 — not on some unlucky subset.
        """
        assert ARTIFACT_SCHEMAS_STEM < "metabase"
        assert ARTIFACT_SCHEMAS_STEM < "atlan-connectors-metabase"

    def test_stem_agrees_with_the_validation_loader(self) -> None:
        """The guard, the loader and form discovery name one file.

        ``application_sdk.validation.sources`` reads the same file this module
        tells form discovery to skip. They are spelled in two modules because
        ``_generated_tree`` stays import-light, so the join is pinned here
        instead: a rename that updated only one side would put the SDK back to
        serving a schema document as a setup form.
        """
        from application_sdk.validation.sources import ARTIFACT_SCHEMAS_FILENAME

        assert ARTIFACT_SCHEMAS_FILENAME == f"{ARTIFACT_SCHEMAS_STEM}.json"


class TestPickFormConfigMap:
    """Name-first, then the filtered sorted scan."""

    def test_skips_artifact_schemas_and_serves_the_form(self, tmp_path: Path) -> None:
        """The FND-1682 tree: K016 adopted, form stem sorting after it."""
        _write(
            tmp_path,
            "artifact_schemas.json",
            "atlan-connectors-metabase.json",
            "manifest.json",
            "metabase.json",
        )
        assert pick_form_configmap(tmp_path, "metabase") == tmp_path / "metabase.json"

    def test_entrypoint_named_form_wins_over_an_alphabetical_sibling(
        self, tmp_path: Path
    ) -> None:
        """A new non-form sibling cannot displace the form it is named for.

        ``aaa-unknown-sibling`` stands in for whatever the toolkit emits next:
        it is on no exclusion list, and it sorts first. Name-first is what makes
        that harmless rather than a re-run of FND-1682.
        """
        _write(tmp_path, "aaa-unknown-sibling.json", "manifest.json", "metabase.json")
        assert pick_form_configmap(tmp_path, "metabase") == tmp_path / "metabase.json"

    def test_falls_back_to_scan_when_form_is_not_named_for_the_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """A connector's ``crawler`` entry point emits ``<source>-crawler.json``.

        The named file does not exist, so the filtered scan answers — dropping
        it would 404 exactly the apps the fallback exists for.
        """
        _write(
            tmp_path,
            "atlan-connectors-snowflake.json",
            "manifest.json",
            "snowflake-crawler.json",
        )
        assert (
            pick_form_configmap(tmp_path, "crawler")
            == tmp_path / "snowflake-crawler.json"
        )

    def test_returns_none_when_every_sibling_is_excluded(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "artifact_schemas.json",
            "atlan-connectors-snowflake.json",
            "manifest.json",
        )
        assert pick_form_configmap(tmp_path, "crawler") is None

    def test_returns_none_for_a_missing_directory(self, tmp_path: Path) -> None:
        assert pick_form_configmap(tmp_path / "nope", "crawler") is None

    def test_entrypoint_named_after_a_non_form_sibling_falls_back(
        self, tmp_path: Path
    ) -> None:
        """The name-first step never re-admits an excluded file.

        An entry point called ``manifest`` would otherwise name its way past the
        exclusion and serve the DAG as the form.
        """
        _write(tmp_path, "manifest.json", "metabase.json")
        assert pick_form_configmap(tmp_path, "manifest") == tmp_path / "metabase.json"


class TestFormConfigMapWithArtifactSchemas:
    """The layout-aware wrapper inherits both fixes."""

    def test_flat_app_serves_its_form_not_the_schemas(self, tmp_path: Path) -> None:
        _write(tmp_path, "artifact_schemas.json", "manifest.json", "metabase.json")
        assert generated_layout(tmp_path) == "single"
        assert form_configmap(tmp_path, "metabase") == tmp_path / "metabase.json"

    def test_bundle_serves_the_entrypoint_form_not_the_schemas(
        self, tmp_path: Path
    ) -> None:
        nested = tmp_path / "crawler"
        _write(
            nested, "artifact_schemas.json", "manifest.json", "snowflake-crawler.json"
        )
        assert generated_layout(tmp_path) == "multi"
        assert form_configmap(tmp_path, "crawler") == nested / "snowflake-crawler.json"
