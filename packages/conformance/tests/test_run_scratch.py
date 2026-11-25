"""Meta-tests for the P-series run-scratch check (P067, FND-5364).

P067 flags ``tempfile.gettempdir()``, and ``tempfile.mkdtemp`` / ``mkstemp``
with no directory, in app code: the workflow sandbox refuses ``tempfile``,
``cleanup_files`` never removes the system temp dir, and a ``/tmp`` path has
no object-store mapping.  ``App.run_scratch_dir`` is the replacement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conformance.suite.checks import run_scratch
from conformance.suite.checks.run_scratch import SERIES, scan_all, scan_path, scan_text


def _rule(src: str, file: str = "app/workflow.py") -> list:
    return [f for f in scan_text(src, file) if f.rule_id == "P067"]


def test_series_letter() -> None:
    assert SERIES == "P"


# ── fires ────────────────────────────────────────────────────────────────────


def test_fires_on_the_fleet_shape_in_workflow_code() -> None:
    """The common fleet shape: a default output path built in ``run()``."""
    src = (
        "import tempfile\n"
        "from pathlib import Path\n"
        "class MyApp(App):\n"
        "    async def run(self, input):\n"
        "        out = Path(tempfile.gettempdir()) / 'my-app' / self.run_id\n"
    )
    [finding] = _rule(src)
    assert finding.line == 5
    assert "tempfile.gettempdir" in finding.message
    assert "self.run_scratch_dir" in finding.message
    assert "dir=" not in finding.message


def test_fires_at_module_level() -> None:
    src = "import tempfile\nTEMP = tempfile.gettempdir()\n"
    assert len(_rule(src)) == 1


def test_fires_on_module_alias_and_direct_imports() -> None:
    src = (
        "import tempfile as tf\n"
        "from tempfile import gettempdir, gettempdirb as gb, mkdtemp as md\n"
        "a = tf.gettempdir()\n"
        "b = gettempdir()\n"
        "c = gb()\n"
        "d = md()\n"
    )
    assert [f.line for f in _rule(src)] == [3, 4, 5, 6]


@pytest.mark.parametrize(
    "call",
    [
        "tempfile.mkdtemp()",
        "tempfile.mkdtemp(prefix='x-')",
        "tempfile.mkdtemp('-s', 'p-')",
        "tempfile.mkdtemp(dir=None)",
        "tempfile.mkdtemp('-s', 'p-', None)",
        "tempfile.mkstemp(suffix='.csv')",
        "tempfile.mkstemp(text=True)",
    ],
)
def test_fires_on_a_maker_with_no_directory(call: str) -> None:
    [finding] = _rule(f"import tempfile\nx = {call}\n")
    assert "dir=self.run_scratch_dir" in finding.message


# ── silent ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        "tempfile.mkdtemp(dir=self.run_scratch_dir)",
        "tempfile.mkdtemp('-s', 'p-', scratch)",
        "tempfile.mkstemp(suffix='.csv', dir=out)",
        "tempfile.mkdtemp(**opts)",
        "tempfile.mkstemp(*args)",
        "tempfile.TemporaryDirectory()",
        "tempfile.NamedTemporaryFile()",
    ],
)
def test_silent_when_a_directory_is_named_or_self_cleaning(call: str) -> None:
    assert _rule(f"import tempfile\nx = {call}\n") == []


def test_silent_when_the_name_is_not_tempfile() -> None:
    """Receiver proof: a parameter or local shadowing ``tempfile`` is not it."""
    src = (
        "import tempfile\n"
        "def f(tempfile):\n"
        "    return tempfile.gettempdir()\n"
        "def g():\n"
        "    gettempdir = lambda: '/x'\n"
        "    return gettempdir()\n"
    )
    assert _rule(src) == []


def test_silent_without_a_tempfile_import() -> None:
    src = "x = tempfile.gettempdir()\ny = gettempdir()\n"
    assert _rule(src) == []


def test_silent_on_a_same_named_attribute_of_another_module() -> None:
    src = "import mylib\nx = mylib.gettempdir()\n"
    assert _rule(src) == []


def test_inline_suppression() -> None:
    src = (
        "import tempfile\n"
        "x = tempfile.gettempdir()  # conformance: ignore[P067] host-wide cache\n"
    )
    [finding] = _rule(src)
    assert finding.suppressed


# ── SDK-version gate ─────────────────────────────────────────────────────────


def _repo(tmp_path: Path, sdk_version: str | None) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text(
        "import tempfile\nT = tempfile.gettempdir()\n"
    )
    if sdk_version is not None:
        (tmp_path / "uv.lock").write_text(
            f'[[package]]\nname = "atlan-application-sdk"\nversion = "{sdk_version}"\n'
        )
    return tmp_path


@pytest.mark.parametrize(
    ("sdk_version", "expected"),
    [
        (None, 0),
        ("3.58.1", 0),
        (".".join(map(str, run_scratch.RUN_SCRATCH_SDK_FLOOR)), 1),
        ("4.0.0", 1),
    ],
)
def test_the_check_is_silent_below_the_sdk_floor(
    tmp_path: Path, sdk_version: str | None, expected: int
) -> None:
    root = _repo(tmp_path, sdk_version)
    path = root / "app" / "x.py"
    assert len(scan_path(path, root)) == expected
    assert len(scan_all([path], root)) == expected
