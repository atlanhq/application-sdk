"""The reviewer's first turn, assembled rather than discovered.

The failures guarded here are the ones that make a review quietly worse rather
than visibly broken: a symbol attributed to the wrong function, so the reviewer
quotes the wrong code as evidence; a caller list silently truncated, so a
blocker is sized as a nit; a pack that grows until the reviewer comments on code
the PR did not touch.
"""

from __future__ import annotations

import pathlib
import sys
import textwrap

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sdk_loop_pack as pack_mod  # noqa: E402
from sdk_loop_pack import (  # noqa: E402
    build_pack,
    find_callers,
    nearby_tests,
    parse_diff,
    render,
    touched_symbols,
)
from sdk_loop_routing import load_routing  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[3]
ROUTING_DATA = REPO / ".mothership/pr-loop/data/agents.yaml"


DIFF = """\
diff --git a/application_sdk/app/base.py b/application_sdk/app/base.py
--- a/application_sdk/app/base.py
+++ b/application_sdk/app/base.py
@@ -10,6 +10,8 @@ class App:
     def existing(self):
         return 1
+    def added_method(self):
+        return 2
@@ -40,3 +42,4 @@ def module_level():
     pass
+CONSTANT = 3
diff --git a/tests/unit/test_base.py b/tests/unit/test_base.py
--- a/tests/unit/test_base.py
+++ b/tests/unit/test_base.py
@@ -1,2 +1,3 @@
 import pytest
+# new line
"""


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------


def test_parse_diff_reports_files_and_added_line_numbers() -> None:
    files = parse_diff(DIFF)
    by_path = {f.path: f for f in files}
    assert set(by_path) == {"application_sdk/app/base.py", "tests/unit/test_base.py"}
    # 12,13 from the first hunk; the second starts at +42 and its one context
    # line pushes the addition to 43. Asserted as literals so a regression in
    # hunk-header arithmetic shows up as a wrong line, not a plausible one.
    assert by_path["application_sdk/app/base.py"].added == (12, 13, 43)
    assert by_path["tests/unit/test_base.py"].is_test


def test_line_numbers_come_from_hunk_headers_not_a_running_count() -> None:
    """A counted offset drifts silently on a truncated patch, and the reviewer
    then gets symbol attributions that are subtly wrong — worse than none,
    because it quotes the wrong code as evidence."""
    truncated = DIFF[: DIFF.index("@@ -40")]
    files = parse_diff(truncated)
    assert files[0].added == (12, 13)


def test_a_deleted_file_is_marked_not_dropped() -> None:
    """A deleted file is a fact the reviewer needs — it is how a removed public
    export looks in a diff — but it has no symbols to resolve."""
    deleted = textwrap.dedent(
        """\
        diff --git a/application_sdk/gone.py b/application_sdk/gone.py
        --- a/application_sdk/gone.py
        +++ /dev/null
        @@ -1,2 +0,0 @@
        -x = 1
        """
    )
    files = parse_diff(deleted)
    assert len(files) == 1
    assert files[0].is_deleted


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------


def test_symbols_are_resolved_by_parsing_not_by_hunk_hint(tmp_path) -> None:
    """Git's `@@` function hint reports the nearest preceding `def` textually,
    which is wrong for methods and nested functions. Parsing gets the enclosing
    definition right, and the reviewer inherits it as fact either way."""
    src = tmp_path / "application_sdk" / "app"
    src.mkdir(parents=True)
    (src / "base.py").write_text(
        textwrap.dedent(
            """\
            class App:
                def existing(self):
                    return 1

                def added_method(self):
                    return 2


            def module_level():
                pass
            """
        ),
        encoding="utf-8",
    )
    changed = parse_diff(
        "diff --git a/application_sdk/app/base.py b/application_sdk/app/base.py\n"
        "--- a/application_sdk/app/base.py\n"
        "+++ b/application_sdk/app/base.py\n"
        "@@ -4,0 +5,2 @@\n"
        "+    def added_method(self):\n"
        "+        return 2\n"
    )
    symbols = touched_symbols(tmp_path, changed)
    names = {s.qualname for s in symbols}
    assert "App.added_method" in names, "the enclosing method was not resolved"
    assert "App.existing" not in names, "an untouched sibling was attributed"


def test_a_file_that_does_not_parse_is_skipped_not_guessed(tmp_path) -> None:
    """Either it is not Python or it is syntactically broken, and in the second
    case the review has a far louder finding available than a symbol list."""
    (tmp_path / "broken.py").write_text("def (: oops\n", encoding="utf-8")
    changed = parse_diff(
        "diff --git a/broken.py b/broken.py\n--- a/broken.py\n+++ b/broken.py\n"
        "@@ -0,0 +1 @@\n+def (: oops\n"
    )
    assert touched_symbols(tmp_path, changed) == ()


def test_the_symbol_list_is_capped_and_keeps_the_specific_ones(tmp_path) -> None:
    """When the cap bites, a method is worth more than the class holding it."""
    body = "\n".join(
        f"class C{i}:\n    def m{i}(self):\n        return {i}\n" for i in range(60)
    )
    (tmp_path / "many.py").write_text(body, encoding="utf-8")
    total_lines = len(body.splitlines())
    changed = parse_diff(
        "diff --git a/many.py b/many.py\n--- a/many.py\n+++ b/many.py\n"
        f"@@ -0,0 +1,{total_lines} @@\n"
        + "\n".join(f"+{ln}" for ln in body.splitlines())
    )
    symbols = touched_symbols(tmp_path, changed)
    assert len(symbols) == pack_mod.MAX_SYMBOLS
    assert all("." in s.qualname for s in symbols), "the cap kept classes over methods"


# ---------------------------------------------------------------------------
# Callers — the input severity depends on
# ---------------------------------------------------------------------------


def test_callers_exclude_the_defining_file(tmp_path) -> None:
    (tmp_path / "a.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from a import target\ntarget()\n", encoding="utf-8")
    changed = parse_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n"
        "+def target():\n+    return 1\n"
    )
    symbols = touched_symbols(tmp_path, changed)
    callers = find_callers(tmp_path, symbols)
    assert callers["target"] == ("b.py",)


def test_the_caller_list_is_capped(tmp_path) -> None:
    """Uncapped this is a search result, and the reviewer does not need to read
    one to size a finding."""
    (tmp_path / "a.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    for i in range(pack_mod.MAX_CALLERS_PER_SYMBOL + 4):
        (tmp_path / f"c{i}.py").write_text("target()\n", encoding="utf-8")
    changed = parse_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n"
        "+def target():\n+    return 1\n"
    )
    callers = find_callers(tmp_path, touched_symbols(tmp_path, changed))
    assert len(callers["target"]) == pack_mod.MAX_CALLERS_PER_SYMBOL


def test_nearby_tests_are_surfaced_so_untested_is_checkable(tmp_path) -> None:
    """An unfounded missing-tests finding is among the most expensive false
    positives available: plausible, tedious to refute, and refuting it is the
    author's job."""
    (tmp_path / "application_sdk").mkdir()
    (tmp_path / "application_sdk" / "storage.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    tests = tmp_path / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_storage.py").write_text("import pytest\n", encoding="utf-8")
    (tests / "test_unrelated.py").write_text("import pytest\n", encoding="utf-8")
    changed = parse_diff(
        "diff --git a/application_sdk/storage.py b/application_sdk/storage.py\n"
        "--- a/application_sdk/storage.py\n+++ b/application_sdk/storage.py\n"
        "@@ -0,0 +1 @@\n+x = 1\n"
    )
    found = nearby_tests(tmp_path, changed)
    assert "tests/unit/test_storage.py" in found
    assert "tests/unit/test_unrelated.py" not in found


# ---------------------------------------------------------------------------
# Assembly and rendering
# ---------------------------------------------------------------------------


def test_the_pack_routes_and_sizes_itself() -> None:
    routing = load_routing(ROUTING_DATA)
    pack = build_pack(repo=REPO, diff=DIFF, scope="full", routing=routing)
    assert "correctness" in pack.agents
    assert "reachability" in pack.agents, "an `always` condition did not fire"
    assert pack.mode == "single_pass"


def test_a_large_diff_asks_for_the_split_mode() -> None:
    big = "diff --git a/application_sdk/x.py b/application_sdk/x.py\n"
    big += "--- a/application_sdk/x.py\n+++ b/application_sdk/x.py\n@@ -0,0 +1,900 @@\n"
    big += "\n".join(f"+line{i}" for i in range(900))
    routing = load_routing(ROUTING_DATA)
    pack = build_pack(repo=REPO, diff=big, scope="full", routing=routing)
    assert pack.mode == "per_module"
    assert "past the size where a single pass finds things" in render(
        pack, "correctness"
    )


def test_config_only_touches_pull_in_ci_config() -> None:
    diff = (
        "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n"
        "--- a/.github/workflows/x.yml\n+++ b/.github/workflows/x.yml\n"
        "@@ -0,0 +1 @@\n+on: push\n"
    )
    routing = load_routing(ROUTING_DATA)
    pack = build_pack(repo=REPO, diff=diff, scope="full", routing=routing)
    assert pack.touches_config
    assert "ci-config" in pack.agents


def test_truncation_is_stated_rather_than_silent() -> None:
    """A pack that silently truncated lets the reviewer conclude a symbol has no
    callers when the list was simply cut — turning a blocker into a nit for a
    reason nobody can see afterwards."""
    routing = load_routing(ROUTING_DATA)
    pack = build_pack(repo=REPO, diff=DIFF, scope="full", routing=routing)
    pack.truncated = ("symbol list capped at 40",)
    assert "## Truncated" in render(pack, "correctness")


def test_the_gate_section_says_not_to_restate_it() -> None:
    """Restating a blocking CI finding costs a round and tells the author
    nothing they were not already told."""
    routing = load_routing(ROUTING_DATA)
    pack = build_pack(
        repo=REPO, diff=DIFF, scope="full", routing=routing, gate="- L001 at x.py:3"
    )
    rendered = render(pack, "correctness")
    assert "do not restate" in rendered.lower()
    assert "L001" in rendered


def test_the_reference_caveat_is_stated() -> None:
    """Name-based resolution is approximate. A reviewer told so will verify a
    blocker and accept a nit, which is the right allocation of effort."""
    routing = load_routing(ROUTING_DATA)
    pack = build_pack(repo=REPO, diff=DIFF, scope="full", routing=routing)
    if pack.symbols:
        assert "resolved by name" in render(pack, "correctness")


@pytest.mark.parametrize("agent", ["correctness", "quality", "structure"])
def test_the_pack_names_the_specialist(agent: str) -> None:
    """`REVIEW.md` says which specialist you are arrives in context. Inferring
    it costs a turn and can be wrong."""
    routing = load_routing(ROUTING_DATA)
    pack = build_pack(repo=REPO, diff=DIFF, scope="full", routing=routing)
    assert render(pack, agent).startswith(f"You are the {agent} specialist.")


def test_a_deleted_file_still_counts_its_removed_lines() -> None:
    """A deleted file has no `+++ b/…` line, so the path stays unset while its
    removals are still real changed lines. Counting them as zero would hand a
    900-line deletion the single-pass depth and review it as if it were small —
    and a large deletion is exactly where a removed public export hides.
    """
    body = "\n".join(f"-line{i}" for i in range(900))
    diff = (
        "diff --git a/application_sdk/gone.py b/application_sdk/gone.py\n"
        "--- a/application_sdk/gone.py\n+++ /dev/null\n@@ -1,900 +0,0 @@\n"
        + body
        + "\n"
    )
    files = parse_diff(diff)
    assert files[0].removed_count == 900

    routing = load_routing(ROUTING_DATA)
    pack = build_pack(repo=REPO, diff=diff, scope="full", routing=routing)
    assert pack.changed_lines == 900
    assert pack.mode == "per_module", "a large deletion was sized as a small change"
