"""Meta-tests for the P-series portability check (P046, FND-924).

P046 flags ``Path.read_text()`` / ``Path.write_text()`` called with no
``encoding=``, which decode and encode using the *locale's* encoding — UTF-8 on
the Linux containers the SDK ships on and on macOS, cp1252 on Windows, which the
SDK's unit matrix runs.

Two properties of the defect drive what is tested here:

* **Assert on the argument, never on a round trip.**  cp1252 encodes ``é`` and
  ``—`` perfectly well and has no mapping for ``→`` or ``✓``, so a round-trip
  fixture passes against this bug on every UTF-8 platform — that is, on every leg
  that stays green when the Windows legs go red.  Every test below inspects the
  ``encoding=`` argument's presence, and none writes a file.
* **The multi-line call form is the shape a regex scan loses.**  The first survey
  of this defect used ``write_text([^)]*)``, which cannot match a call whose
  arguments wrap, and so missed exactly the multi-line calls most likely to be
  interesting.  The AST cannot lose them, and
  :func:`test_p046_fires_on_a_multi_line_write_text` pins that.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks._ast_common import EXCLUDE_DIRS
from conformance.suite.checks.text_io_encoding import (
    RULE_ID,
    SERIES,
    discover,
    scan_path,
    scan_text,
)
from conformance.suite.schema.findings import Finding


def _rule(src: str, file: str = "app/x.py") -> list[Finding]:
    """P046 findings from a per-file scan of *src* at path *file*."""
    return [f for f in scan_text(src, file) if f.rule_id == RULE_ID]


def test_series_letter() -> None:
    assert SERIES == "P"
    assert RULE_ID == "P046"


# ── Fires — the encoding argument is absent entirely ─────────────────────────


def test_p046_fires_on_bare_read_text() -> None:
    fs = _rule("from pathlib import Path\nPath('x').read_text()\n")
    assert len(fs) == 1 and fs[0].line == 2


def test_p046_fires_on_bare_write_text() -> None:
    fs = _rule("from pathlib import Path\nPath('x').write_text(body)\n")
    assert len(fs) == 1 and fs[0].line == 2


def test_p046_fires_on_read_text_with_only_errors_keyword() -> None:
    """``errors=`` is not ``encoding=`` — the locale still picks the codec."""
    assert len(_rule('p.read_text(errors="ignore")\n')) == 1


def test_p046_fires_on_a_multi_line_write_text() -> None:
    """The wrapped-argument form a regex scan cannot see.

    ``write_text([^)]*)`` matched none of the three multi-line calls in the
    original survey, which is how they went unreported.  The finding must anchor
    to the call's own first line.
    """
    src = (
        "from pathlib import Path\n"
        "\n"
        "(out_dir / 'report.json').write_text(\n"
        "    json.dumps(payload, indent=2)\n"
        "    + '\\n',\n"
        ")\n"
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].line == 3


def test_p046_fires_on_a_multi_line_read_text() -> None:
    src = "text = (\n    some_dir\n    / 'notes.md'\n).read_text()\n"
    assert len(_rule(src)) == 1


def test_p046_fires_once_per_call_site() -> None:
    src = "a = p.read_text()\nb = q.read_text()\np.write_text(a)\n"
    assert [f.line for f in _rule(src)] == [1, 2, 3]


# ── Fires with the read_bytes() remedy when the read feeds a bytes parser ────


def test_p046_names_read_bytes_when_the_read_feeds_orjson_loads() -> None:
    """``orjson.loads`` takes bytes, so ``encoding=`` is the wrong fix here."""
    (finding,) = _rule("import orjson\nm = orjson.loads(path.read_text())\n")
    assert "read_bytes()" in finding.message
    assert "orjson.loads()" in finding.message


def test_p046_names_read_bytes_for_stdlib_json_loads() -> None:
    (finding,) = _rule("import json\nm = json.loads(path.read_text())\n")
    assert "read_bytes()" in finding.message
    assert "json.loads()" in finding.message


def test_p046_names_read_bytes_for_a_bare_imported_loads() -> None:
    (finding,) = _rule("from orjson import loads\nm = loads(path.read_text())\n")
    assert "read_bytes()" in finding.message


def test_p046_names_the_encoding_kwarg_when_no_parser_consumes_the_read() -> None:
    """A plain read gets the ``encoding=``/``safe_read_text`` remedy, not bytes."""
    (finding,) = _rule("stored = sidecar.read_text().strip()\n")
    assert "read_bytes()" not in finding.message
    assert 'encoding="utf-8"' in finding.message
    assert "safe_read_text" in finding.message


# ── Silent — an encoding is supplied, in any accepted shape ──────────────────


def test_p046_silent_on_read_text_encoding_keyword() -> None:
    assert _rule('p.read_text(encoding="utf-8")\n') == []


def test_p046_silent_on_read_text_positional_encoding() -> None:
    """``encoding`` is ``read_text``'s first positional parameter."""
    assert _rule('p.read_text("utf-8")\n') == []


def test_p046_silent_on_write_text_encoding_keyword() -> None:
    assert _rule('p.write_text(body, encoding="utf-8")\n') == []


def test_p046_silent_on_write_text_positional_encoding() -> None:
    """``encoding`` is ``write_text``'s *second* positional parameter."""
    assert _rule('p.write_text(body, "utf-8")\n') == []


def test_p046_fires_on_write_text_with_only_the_payload_positional() -> None:
    """One positional is the payload, not the encoding — the mirror of the above."""
    assert len(_rule("p.write_text(body)\n")) == 1


def test_p046_silent_on_a_multi_line_call_whose_encoding_wraps() -> None:
    """The false-positive twin of the regex miss.

    ``grep -v encoding=`` reports a multi-line call as unfixed when its
    ``encoding=`` sits on a later line — which is how two already-correct sites
    were listed as defects in the original survey.  The AST must not repeat it.
    """
    src = (
        "Path(args.output_json).write_text(\n"
        "    orjson.dumps(report).decode(),\n"
        '    encoding="utf-8",\n'
        ")\n"
    )
    assert _rule(src) == []


def test_p046_silent_on_a_kwargs_splat() -> None:
    """An opaque splat may carry ``encoding``; a false positive is worse than a miss."""
    assert _rule("p.write_text(body, **opts)\n") == []
    assert _rule("p.read_text(**opts)\n") == []


def test_p046_silent_on_the_distribution_read_text_lookalike() -> None:
    """``importlib.metadata.Distribution.read_text(filename)`` does not decode-by-locale.

    It takes a *filename* positionally and returns ``None`` when absent.  The
    positional argument is what clears it, so no per-site exemption marker is
    needed — unlike the suite's decode-safety read guard, which needs one.
    """
    src = "import importlib.metadata\n" 'top = dist.read_text("top_level.txt")\n'
    assert _rule(src) == []


def test_p046_silent_on_read_bytes() -> None:
    assert _rule("raw = p.read_bytes()\nq.write_bytes(raw)\n") == []


def test_p046_silent_on_an_unrelated_method_name() -> None:
    assert _rule("p.read_texture()\np.write_textual(x)\n") == []


def test_p046_silent_on_unparseable_source() -> None:
    assert _rule("def broken(:\n") == []


# ── Suppression ──────────────────────────────────────────────────────────────


def test_p046_suppressed_inline() -> None:
    src = "p.read_text()  # conformance: ignore[P046] ASCII sidecar by construction\n"
    (finding,) = _rule(src)
    assert finding.suppressed
    assert finding.suppression_justification == "ASCII sidecar by construction"


def test_p046_suppressed_by_the_comment_line_above() -> None:
    src = (
        "# conformance: ignore[P046] ASCII sidecar by construction\n" "p.read_text()\n"
    )
    (finding,) = _rule(src)
    assert finding.suppressed


# ── Discovery ────────────────────────────────────────────────────────────────


def test_discover_reaches_the_packages_conformance_sources(tmp_path: Path) -> None:
    """The shared walk drops any ``conformance`` component; P046 must not.

    The rule governs ``packages/conformance/**`` as well as the SDK package, and
    the conformance package's own directory name is on the shared walk's
    exclusion list — so a rule reusing that walk unchanged would silently never
    see the sources it is scoped to.
    """
    assert "conformance" in EXCLUDE_DIRS  # premise: the shared walk drops it

    (tmp_path / "application_sdk").mkdir()
    (tmp_path / "application_sdk" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    pkg = tmp_path / "packages" / "conformance" / "conformance"
    pkg.mkdir(parents=True)
    (pkg / "scan.py").write_text("y = 2\n", encoding="utf-8")

    found = {p.relative_to(tmp_path).as_posix() for p in discover(tmp_path)}
    assert found == {
        "application_sdk/mod.py",
        "packages/conformance/conformance/scan.py",
    }


def test_discover_still_excludes_tests_inside_a_package(tmp_path: Path) -> None:
    """``tests/`` stays out — only the ``conformance`` exclusion is lifted.

    Scoping the rule away from ``tests/`` is deliberate: nearly every test-side
    match is a test writing its own ASCII fixture and reading it back, where the
    locale cannot bite, so enforcing there buys a large sweep and no risk
    reduction.
    """
    tests_dir = tmp_path / "packages" / "conformance" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "helper.py").write_text("z = 3\n", encoding="utf-8")

    assert discover(tmp_path) == []


def test_discover_is_inert_without_a_packages_dir(tmp_path: Path) -> None:
    """A consumer app has no ``packages/``; discovery must match the shared walk."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")

    assert [p.name for p in discover(tmp_path)] == ["main.py"]


def test_scan_path_reports_a_repo_relative_uri(tmp_path: Path) -> None:
    src = tmp_path / "pkg" / "mod.py"
    src.parent.mkdir()
    src.write_text("p.read_text()\n", encoding="utf-8")

    (finding,) = scan_path(src, tmp_path)
    assert finding.file == str(Path("pkg") / "mod.py")
    assert finding.line == 1


def test_scan_path_survives_an_undecodable_file(tmp_path: Path) -> None:
    """A stray non-UTF-8 byte must skip the file, not abort the run.

    ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``, and the runner
    wraps neither ``discover()`` nor ``scan_path()`` — so an unguarded read here
    would take down every later series in the same run and report nothing.
    """
    bad = tmp_path / "bad.py"
    bad.write_bytes(b"p.read_text()  # \xff\xfe not utf-8\n")

    assert scan_path(bad, tmp_path) == []


# ── open() — fires on a text-mode call with no encoding ──────────────────────


def test_p046_fires_on_bare_builtin_open() -> None:
    """No mode argument means mode ``"r"``, which is text."""
    fs = _rule("with open(path) as fh:\n    body = fh.read()\n")
    assert len(fs) == 1 and fs[0].line == 1


def test_p046_fires_on_explicit_text_modes() -> None:
    for mode in ('"r"', '"w"', '"a"', '"r+"', '"w+"', '"rt"'):
        assert len(_rule(f"open(p, {mode})\n")) == 1, mode


def test_p046_fires_on_path_open_with_no_arguments() -> None:
    """``Path.open()`` takes ``mode`` first — absent means text."""
    assert len(_rule("with yaml_file.open() as fh:\n    pass\n")) == 1


def test_p046_fires_on_path_open_with_a_text_mode() -> None:
    assert len(_rule('with history_path.open("a") as fh:\n    pass\n')) == 1


def test_p046_fires_on_open_carrying_newline_but_no_encoding() -> None:
    """``newline=`` is a text-mode-only kwarg — it confirms text, not encoding."""
    assert len(_rule('open(file_path, "w", newline="")\n')) == 1


def test_p046_fires_on_io_and_aiofiles_open() -> None:
    """Signature-compatible aliases of the builtin, so the mode index matches."""
    assert len(_rule("io.open(path)\n")) == 1
    assert len(_rule("aiofiles.open(path)\n")) == 1


def test_p046_fires_on_compressed_open_only_in_text_mode() -> None:
    """``gzip``/``bz2``/``lzma`` default to binary — text needs an explicit ``t``."""
    for module in ("gzip", "bz2", "lzma"):
        assert len(_rule(f'{module}.open(path, "wt")\n')) == 1, module
        assert _rule(f'{module}.open(path, "wb")\n') == [], module
        assert _rule(f"{module}.open(path)\n") == [], module


def test_p046_fires_on_a_text_mode_tempfile() -> None:
    """``NamedTemporaryFile`` defaults to ``"w+b"``; a text mode opts into the locale."""
    assert len(_rule('tempfile.NamedTemporaryFile(mode="w")\n')) == 1
    assert _rule('tempfile.NamedTemporaryFile(mode="wb")\n') == []
    assert _rule("tempfile.NamedTemporaryFile(delete=False)\n") == []


def test_p046_fires_on_a_bare_imported_tempfile_factory() -> None:
    """``from tempfile import NamedTemporaryFile`` is as common as the dotted form."""
    src = 'from tempfile import NamedTemporaryFile\nNamedTemporaryFile(mode="w")\n'
    assert len(_rule(src)) == 1


def test_p046_fires_on_a_builtin_open_whose_mode_is_dynamic() -> None:
    """``open`` is an unambiguous name, so an unreadable mode stays a real risk.

    This is the deliberate asymmetry with ``<expr>.open`` below: there is no
    lookalike named plain ``open``, so the conservative direction is to flag.
    """
    assert len(_rule("open(path, mode)\n")) == 1


# ── open() — silent ──────────────────────────────────────────────────────────


def test_p046_silent_on_binary_modes() -> None:
    for mode in ('"rb"', '"wb"', '"ab"', '"r+b"', '"w+b"'):
        assert _rule(f"open(p, {mode})\n") == [], mode
    assert _rule('with path.open("rb") as fh:\n    pass\n') == []


def test_p046_silent_on_open_with_an_encoding() -> None:
    assert _rule('open(path, encoding="utf-8")\n') == []
    assert _rule('open(path, "w", encoding="utf-8")\n') == []
    assert _rule('path.open(encoding="utf-8")\n') == []
    assert _rule('path.open("a", encoding="utf-8")\n') == []


def test_p046_silent_on_open_with_a_positional_encoding() -> None:
    """``open(file, mode, buffering, encoding)`` — index 3; ``Path.open`` — index 2."""
    assert _rule('open(path, "r", -1, "utf-8")\n') == []
    assert _rule('path.open("r", -1, "utf-8")\n') == []


def test_p046_silent_on_os_open_which_returns_a_descriptor() -> None:
    """``os.open`` yields an int fd — no decode step exists to get wrong."""
    src = "fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)\n"
    assert _rule(src) == []


def test_p046_silent_on_archive_and_database_opens() -> None:
    """These ``open``s return archive members or handles, never a decoded stream."""
    assert _rule('tarfile.open(tmp_name, "r:gz")\n') == []
    assert _rule("zipfile.open(name)\n") == []
    assert _rule('codecs.open(path, "r")\n') == []
    assert _rule("webbrowser.open(url)\n") == []
    assert _rule("shelve.open(path)\n") == []


def test_p046_silent_on_a_zipfile_member_open() -> None:
    """The lookalike that motivates skipping an unreadable ``<expr>.open`` mode.

    ``zf.open(member)`` passes a *member name* where ``Path.open`` takes its
    mode, and the receiver is a local variable, so no receiver check can tell
    them apart.  Treating an unreadable mode as text would report this — a
    binary archive read — as a locale defect.
    """
    src = 'with zf.open(member) as src, target.open("wb") as dst:\n    pass\n'
    assert _rule(src) == []


def test_p046_silent_on_the_safefileops_wrapper() -> None:
    """``SafeFileOps.open`` resolves UTF-8 itself, so its callers are correct.

    Flagging them would report the wrapper's entire reason for existing as the
    defect it was written to prevent.
    """
    assert _rule("with SafeFileOps.open(file_name, mode=mode) as f:\n    pass\n") == []


def test_p046_silent_on_a_kwargs_splat_in_an_open() -> None:
    assert _rule("open(path, **opts)\n") == []


def test_p046_silent_on_an_unrelated_open_ish_name() -> None:
    assert _rule("session.open_connection(host)\n") == []
    assert _rule("cursor.reopen()\n") == []


# ── open() — SpooledTemporaryFile shifts both indices by one ─────────────────


def test_p046_fires_on_a_spooled_tempfile_with_a_keyword_mode() -> None:
    assert len(_rule('tempfile.SpooledTemporaryFile(mode="w")\n')) == 1


def test_p046_fires_on_a_spooled_tempfile_whose_mode_is_positional() -> None:
    """``SpooledTemporaryFile(max_size, mode, ...)`` — the mode is the *second* arg.

    Sharing ``NamedTemporaryFile``'s signature would read ``max_size`` as the
    mode here; a non-string int is unreadable, so the call would be skipped and
    a genuine text-mode temp file would go unreported.
    """
    assert len(_rule('tempfile.SpooledTemporaryFile(1024, "w")\n')) == 1
    assert _rule('tempfile.SpooledTemporaryFile(1024, "w+b")\n') == []


def test_p046_silent_on_a_spooled_tempfile_positional_encoding() -> None:
    """Encoding is index 3 here, not 2 — one past ``NamedTemporaryFile``'s."""
    assert _rule('tempfile.SpooledTemporaryFile(1024, "w", -1, "utf-8")\n') == []
    assert len(_rule('tempfile.SpooledTemporaryFile(1024, "w", -1)\n')) == 1


def test_p046_silent_on_a_bare_spooled_tempfile() -> None:
    """No mode argument means ``"w+b"`` — binary."""
    assert _rule("tempfile.SpooledTemporaryFile(1024)\n") == []
