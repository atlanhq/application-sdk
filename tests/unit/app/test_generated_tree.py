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
    names_entrypoint,
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

    def test_connector_suffix_names_the_entrypoint(self, tmp_path: Path) -> None:
        """``crawler`` is identified by ``snowflake-crawler.json``.

        The connector convention makes the entry point the *role* and the file
        carry the source, so exact-name matching alone would leave the entire
        connector fleet on the last-resort guess. Here the unrecognised sibling
        sorts first and must still lose.
        """
        _write(
            tmp_path,
            "aaa-unknown-sibling.json",
            "atlan-connectors-snowflake.json",
            "manifest.json",
            "snowflake-crawler.json",
        )
        assert (
            pick_form_configmap(tmp_path, "crawler")
            == tmp_path / "snowflake-crawler.json"
        )

    def test_sole_candidate_wins_without_naming_the_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """One eligible file is an identification: nothing else it could be.

        A route/card-split app is this shape — ``metabase.json`` serves an
        ``extract_metadata`` entry point, and no naming rule relates the two.
        """
        _write(
            tmp_path,
            "artifact_schemas.json",
            "atlan-connectors-metabase.json",
            "manifest.json",
            "metabase.json",
        )
        assert (
            pick_form_configmap(tmp_path, "extract-metadata")
            == tmp_path / "metabase.json"
        )

    def test_alphabetical_guess_survives_as_the_compatibility_path(
        self, tmp_path: Path
    ) -> None:
        """Several candidates, none named for the entry point → first by name.

        This step *is* a guess, and it is kept on purpose: it is what serves an
        app whose form name the SDK cannot recognise. Turning this case into a
        ``None`` (a 404) would break apps that work today to defend against a
        sibling the toolkit does not yet emit — a census of the fleet's
        committed ``app/generated`` trees found no directory with two eligible
        files, so the trade buys nothing and costs the un-adopted.

        FND-1682 was never the guess being *reachable*; it was
        ``artifact_schemas.json`` being eligible at all. The endpoint logs a
        warning when it lands here, which is the part that was missing.
        """
        _write(tmp_path, "aaa-unknown-sibling.json", "manifest.json", "metabase.json")
        assert (
            pick_form_configmap(tmp_path, "extract-metadata")
            == tmp_path / "aaa-unknown-sibling.json"
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
        """The naming step never re-admits an excluded file.

        An entry point called ``manifest`` would otherwise name its way past the
        exclusion and serve the DAG as the form. It cannot: the name match runs
        over the *eligible* candidates, never over the directory.
        """
        _write(tmp_path, "manifest.json", "metabase.json")
        assert pick_form_configmap(tmp_path, "manifest") == tmp_path / "metabase.json"


class TestNamesEntrypoint:
    """Which stems count as identifying an entry point."""

    @pytest.mark.parametrize(
        ("stem", "entrypoint"),
        [
            ("bridge", "bridge"),
            ("snowflake-crawler", "crawler"),
            ("postgres-miner", "miner"),
        ],
    )
    def test_recognised_spellings(self, stem: str, entrypoint: str) -> None:
        assert names_entrypoint(stem, entrypoint) is True

    @pytest.mark.parametrize(
        ("stem", "entrypoint"),
        [
            # Substring, not a suffix on a hyphen boundary: a form for some
            # other role must not answer for this one.
            ("crawler-legacy", "crawler"),
            # The hyphen is required — "recrawler" is a different word.
            ("recrawler", "crawler"),
            ("metabase", "extract-metadata"),
        ],
    )
    def test_unrecognised_spellings(self, stem: str, entrypoint: str) -> None:
        assert names_entrypoint(stem, entrypoint) is False


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
