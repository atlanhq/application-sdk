"""Tests for the rule-doc generator's literal-block handling (BLDX-1520).

A naive "split on blank lines, then textwrap.fill every paragraph" pass reflows
RST literal blocks exactly like prose, collapsing every code example in the
generated rule docs onto one run-on line. _split_literal_blocks() detects those
blocks so the renderer can emit them verbatim inside a fenced code block instead.
"""

from __future__ import annotations

import re

from conformance.tools.generate_rule_docs import _split_literal_blocks


def test_plain_prose_is_a_single_non_code_chunk() -> None:
    text = "Just some prose.\n\nA second paragraph."
    segments = _split_literal_blocks(text)
    assert all(not is_code for _, is_code, _ in segments)
    assert "".join(chunk for chunk, _, _ in segments).strip() != ""


def test_double_colon_opens_a_literal_block() -> None:
    text = "Do this::\n\n    import foo\n    foo.bar()\n\nDone."
    segments = _split_literal_blocks(text)
    kinds = [is_code for _, is_code, _ in segments]
    assert kinds == [False, True, False]
    prose_before, code, prose_after = (c for c, _, _ in segments)
    assert prose_before.rstrip().endswith(":")  # "::" -> ":"
    assert not prose_before.rstrip().endswith("::")
    assert code == "import foo\nfoo.bar()"
    assert prose_after.strip() == "Done."
    # Bare "::" carries no language of its own — defaults to python.
    langs = [lang for _, is_code, lang in segments if is_code]
    assert langs == ["python"]


def test_code_block_directive_opens_a_literal_block_and_is_dropped() -> None:
    text = ".. code-block:: python\n\n    class Foo:\n        pass\n\nDone."
    segments = _split_literal_blocks(text)
    kinds = [is_code for _, is_code, _ in segments]
    assert kinds == [True, False]
    code, prose_after = (c for c, _, _ in segments)
    assert code == "class Foo:\n    pass"
    assert ".. code-block::" not in prose_after
    assert prose_after.strip() == "Done."


def test_code_block_directive_honours_an_explicit_non_python_language() -> None:
    text = ".. code-block:: toml\n\n    [tool.conformance]\n    x = 1\n\nDone."
    segments = _split_literal_blocks(text)
    code, is_code, lang = next(s for s in segments if s[1])
    assert lang == "toml"
    assert code == "[tool.conformance]\nx = 1"


def test_blank_line_inside_block_is_preserved() -> None:
    text = "Example::\n\n    import asyncio\n\n    async def main():\n        pass\n"
    segments = _split_literal_blocks(text)
    code = next(chunk for chunk, is_code, _ in segments if is_code)
    assert code == "import asyncio\n\nasync def main():\n    pass"


def test_dedents_to_the_common_indent() -> None:
    text = "Example::\n\n    outer = 1\n        nested = 2\n"
    segments = _split_literal_blocks(text)
    code = next(chunk for chunk, is_code, _ in segments if is_code)
    # Common indent (4 spaces) stripped; relative nesting preserved.
    assert code == "outer = 1\n    nested = 2"


def test_no_block_when_nothing_indented_follows() -> None:
    """A line ending in '::' with no indented follow-up is not a block opener."""
    text = "This looks like RST::\nBut the next line isn't indented."
    segments = _split_literal_blocks(text)
    assert all(not is_code for _, is_code, _ in segments)


def test_bare_double_colon_line_is_not_treated_as_a_block_opener() -> None:
    text = "::\n\n    should not matter\n"
    segments = _split_literal_blocks(text)
    assert all(not is_code for _, is_code, _ in segments)


def test_multiple_separate_blocks_in_one_description() -> None:
    text = (
        "First, replace::\n\n    old_value = 1\n\nwith::\n\n    new_value = 2\n\nDone."
    )
    segments = _split_literal_blocks(text)
    codes = [chunk for chunk, is_code, _ in segments if is_code]
    assert codes == ["old_value = 1", "new_value = 2"]


def test_reassembled_prose_has_no_leftover_double_colons() -> None:
    text = "Do this::\n\n    x = 1\n\nAnd that::\n\n    y = 2\n"
    segments = _split_literal_blocks(text)
    prose = " ".join(chunk for chunk, is_code, _ in segments if not is_code)
    assert "::" not in prose


# ── per-rule files (by-id/) — the context-squeezed single-rule view ────────


def test_by_id_files_are_generated_for_every_rule(tmp_path) -> None:
    from conformance.suite.rules import CATALOG
    from conformance.tools import generate_rule_docs as g

    g.main(["--outdir", str(tmp_path)])
    by_id = tmp_path / "by-id"
    generated = {p.stem for p in by_id.glob("*.md")}
    assert generated == set(CATALOG.keys())


def test_by_id_file_carries_the_same_block_as_the_series_doc(tmp_path) -> None:
    """Shared renderer = the two views can never drift."""
    from conformance.tools import generate_rule_docs as g

    g.main(["--outdir", str(tmp_path)])
    single = (tmp_path / "by-id" / "E010.md").read_text()
    series = (tmp_path / "error-handling.md").read_text()
    block_heading = "## E010 — `AsyncioGatherExceptionsUnexamined`"
    assert block_heading in single and block_heading in series


def test_check_mode_catches_a_missing_by_id_file(tmp_path) -> None:
    import pytest
    from conformance.tools import generate_rule_docs as g

    g.main(["--outdir", str(tmp_path)])
    (tmp_path / "by-id" / "E010.md").unlink()
    with pytest.raises(SystemExit):
        g.main(["--outdir", str(tmp_path), "--check"])


def test_by_id_file_prints_a_directive_for_its_own_rule(tmp_path) -> None:
    """A by-id file is a one-rule fetch, so the suppression directive it prints
    must name that rule — copying a sibling's id leaves the finding live."""
    from conformance.suite.rules import CATALOG
    from conformance.tools import generate_rule_docs as g

    g.main(["--outdir", str(tmp_path)])
    for rule_id in CATALOG:
        text = (tmp_path / "by-id" / f"{rule_id}.md").read_text()
        assert f"ignore[{rule_id}]" in text, rule_id
        # No other rule id may appear inside a suppression directive.
        assert not re.findall(
            r"ignore\[(?!" + re.escape(rule_id) + r"\])[A-Z]\d+\]", text
        ), rule_id


def test_by_id_directive_keeps_the_series_comment_syntax(tmp_path) -> None:
    """K-series directives are ``//``, not ``#`` — the syntax comes from the
    series example, so re-pointing the id must not rewrite it to Python."""
    from conformance.tools import generate_rule_docs as g

    g.main(["--outdir", str(tmp_path)])
    assert (
        "// conformance: ignore[K001]" in (tmp_path / "by-id" / "K001.md").read_text()
    )
    assert "# conformance: ignore[E010]" in (tmp_path / "by-id" / "E010.md").read_text()


def test_check_mode_catches_an_orphaned_by_id_file(tmp_path) -> None:
    """A by-id file whose rule id has left the catalog is invisible to the
    expected-file loop; only an orphan sweep can fail it."""
    import pytest
    from conformance.tools import generate_rule_docs as g

    g.main(["--outdir", str(tmp_path)])
    orphan = tmp_path / "by-id" / "E999.md"
    orphan.write_text("# E999 — a rule that no longer exists\n")
    with pytest.raises(SystemExit):
        g.main(["--outdir", str(tmp_path), "--check"])


def test_regeneration_removes_an_orphaned_by_id_file(tmp_path) -> None:
    """--check's remedy is 'regenerate', so regeneration has to be able to fix
    what --check reports."""
    from conformance.tools import generate_rule_docs as g

    g.main(["--outdir", str(tmp_path)])
    orphan = tmp_path / "by-id" / "E999.md"
    orphan.write_text("# E999 — a rule that no longer exists\n")
    g.main(["--outdir", str(tmp_path)])
    assert not orphan.exists()
    g.main(["--outdir", str(tmp_path), "--check"])
