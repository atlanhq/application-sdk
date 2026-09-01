"""Meta-tests for the P-series atomic-publish check (P050, CONNECT-1126).

P050 flags ``os.open`` calls whose flags carry ``O_TRUNC`` when no enclosing
scope publishes via ``os.replace`` / ``os.rename``.  Two properties of the
defect drive what is tested here:

* **The publish must be at the enclosing scope's own level.**  One checkpoint
  writer with its own ``os.replace`` elsewhere in a module must not clear a
  violating ``os.open`` in a sibling function — the shape that would have
  hidden the original defect, since the module that carried it also carried an
  atomic sidecar writer.
* **The closure pattern passes.**  Chunk workers writing through a descriptor
  while the outer function publishes is the sanctioned chunked-download shape.
"""

from __future__ import annotations

from conformance.suite.checks.atomic_publish import RULE_ID, SERIES, scan_text
from conformance.suite.schema.findings import Finding

_VIOLATION = (
    "import os\n"
    "def download(path):\n"
    "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
    "    os.write(fd, b'x')\n"
    "    os.close(fd)\n"
)


def _rule(src: str, file: str = "application_sdk/x.py") -> list[Finding]:
    """P050 findings from a per-file scan of *src* at path *file*."""
    return [f for f in scan_text(src, file) if f.rule_id == RULE_ID]


def test_series_letter() -> None:
    assert SERIES == "P"
    assert RULE_ID == "P050"


# ── Fires — in-place O_TRUNC write with no publish in scope ──────────────────


def test_p048_fires_on_in_place_o_trunc_open() -> None:
    fs = _rule(_VIOLATION)
    assert len(fs) == 1 and fs[0].line == 3


def test_p048_fires_on_bare_imported_o_trunc() -> None:
    src = (
        "import os\n"
        "from os import O_CREAT, O_TRUNC, O_WRONLY\n"
        "def write(path):\n"
        "    fd = os.open(path, O_WRONLY | O_CREAT | O_TRUNC)\n"
    )
    assert len(_rule(src)) == 1


def test_p048_fires_on_conditional_o_trunc() -> None:
    """The resuming-download shape: O_TRUNC behind a conditional still counts."""
    src = (
        "import os\n"
        "def download(path, resuming):\n"
        "    flags = 0\n"
        "    fd = os.open(path, os.O_WRONLY | (0 if resuming else os.O_TRUNC))\n"
    )
    assert len(_rule(src)) == 1


def test_p048_fires_when_o_trunc_arrives_through_a_flags_variable() -> None:
    """The incident's chunked-download spelling: flags built in a local, then
    ``os.open(path, flags)`` — the call itself never names O_TRUNC."""
    src = (
        "import os\n"
        "def download(path, resuming):\n"
        "    flags = os.O_WRONLY | os.O_CREAT | (0 if resuming else os.O_TRUNC)\n"
        "    fd = os.open(path, flags, 0o600)\n"
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].line == 4


def test_p048_flags_variable_still_passes_with_a_publish() -> None:
    src = (
        "import os\n"
        "def download(part, path):\n"
        "    flags = os.O_WRONLY | os.O_TRUNC\n"
        "    fd = os.open(part, flags)\n"
        "    os.close(fd)\n"
        "    os.replace(part, path)\n"
    )
    assert _rule(src) == []


def test_p048_is_not_cleared_by_a_lambda_that_publishes() -> None:
    src = (
        "import os\n"
        "def download(path):\n"
        "    pub = lambda a, b: os.replace(a, b)\n"
        "    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)\n"
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].line == 4


def test_p048_fires_inside_a_lambda_with_no_enclosing_publish() -> None:
    src = "import os\nf = lambda p: os.open(p, os.O_WRONLY | os.O_TRUNC)\n"
    assert len(_rule(src)) == 1


def test_p048_parameter_shadows_an_outer_tainted_name() -> None:
    """A module-level tainted ``flags`` must not condemn a function whose own
    ``flags`` parameter is a different binding."""
    src = (
        "import os\n"
        "flags = os.O_WRONLY | os.O_TRUNC\n"
        "def f(path, flags):\n"
        "    fd = os.open(path, flags)\n"
    )
    assert _rule(src) == []


def test_p048_clean_rebinding_shadows_an_outer_tainted_name() -> None:
    src = (
        "import os\n"
        "flags = os.O_WRONLY | os.O_TRUNC\n"
        "def f(path):\n"
        "    flags = os.O_WRONLY | os.O_CREAT\n"
        "    fd = os.open(path, flags)\n"
    )
    assert _rule(src) == []


def test_p048_own_scope_taint_wins_over_its_own_clean_rebinding() -> None:
    """Flow-insensitive by design: once a scope taints a name, a later clean
    rebinding in the same scope does not untaint it."""
    src = (
        "import os\n"
        "def f(path):\n"
        "    flags = os.O_WRONLY | os.O_TRUNC\n"
        "    flags = os.O_WRONLY\n"
        "    fd = os.open(path, flags)\n"
    )
    assert len(_rule(src)) == 1


def test_p048_fires_at_module_level() -> None:
    src = "import os\nfd = os.open('x', os.O_WRONLY | os.O_TRUNC)\n"
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].line == 2


def test_p048_is_not_cleared_by_a_sibling_functions_replace() -> None:
    """The shape that would have hidden the original defect: the module that
    carried the in-place download also carried an atomic checkpoint writer."""
    src = (
        "import os\n"
        "def save_state(tmp, state_path):\n"
        "    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
        "    os.close(fd)\n"
        "    os.replace(tmp, state_path)\n"
        "def download(path):\n"
        "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
        "    os.close(fd)\n"
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].line == 7


# ── Passes — the temp-then-replace pattern, and non-truncating opens ─────────


def test_p048_passes_when_same_function_publishes_via_replace() -> None:
    src = (
        "import os\n"
        "def save(tmp, path):\n"
        "    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
        "    os.close(fd)\n"
        "    os.replace(tmp, path)\n"
    )
    assert _rule(src) == []


def test_p048_passes_when_same_function_publishes_via_rename() -> None:
    src = (
        "import os\n"
        "def save(tmp, path):\n"
        "    fd = os.open(tmp, os.O_WRONLY | os.O_TRUNC)\n"
        "    os.rename(tmp, path)\n"
    )
    assert _rule(src) == []


def test_p048_passes_on_the_closure_pattern() -> None:
    """Workers write through a descriptor; the outer function publishes."""
    src = (
        "import os\n"
        "def download(part, path):\n"
        "    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
        "    def worker(chunk):\n"
        "        os.write(fd, chunk)\n"
        "    worker(b'x')\n"
        "    os.close(fd)\n"
        "    os.replace(part, path)\n"
    )
    assert _rule(src) == []


def test_p048_lambda_parameter_shadows_an_outer_tainted_name() -> None:
    """A lambda's parameters are a fresh binding, exactly like a def's."""
    src = (
        "import os\n"
        "flags = os.O_WRONLY | os.O_TRUNC\n"
        "f = lambda path, flags: os.open(path, flags)\n"
    )
    assert _rule(src) == []


def test_p048_enclosing_publish_clears_a_nested_def_violator() -> None:
    """The documented closure allowance: clearance is inherited inward, so a
    publish in the outer function clears an ``O_TRUNC`` open in a nested def
    — the deliberate no-solver ceiling, not a gap."""
    src = (
        "import os\n"
        "def f(tmp, path, target):\n"
        "    def g():\n"
        "        os.open(target, os.O_WRONLY | os.O_TRUNC)\n"
        "    g()\n"
        "    os.replace(tmp, path)\n"
    )
    assert _rule(src) == []


def test_p048_enclosing_publish_clears_a_lambda_violator() -> None:
    """Same allowance, lambda spelling — uniform with the nested-def case."""
    src = (
        "import os\n"
        "def f(tmp, path, target):\n"
        "    write = lambda: os.open(target, os.O_WRONLY | os.O_TRUNC)\n"
        "    write()\n"
        "    os.replace(tmp, path)\n"
    )
    assert _rule(src) == []


def test_p048_nested_class_publish_does_not_clear_an_outer_violator() -> None:
    """A ``class`` body is its own namespace, so its ``os.replace`` is not the
    enclosing function's publish.

    Same defect class as the lambda instance already fixed: excluding only
    ``def`` / ``lambda`` from "own level" let an atomic helper parked in a
    nested class clear a violating open beside it — a false negative on the
    exact shape the rule exists to catch.
    """
    src = (
        "import os\n"
        "def f(target, tmp, path):\n"
        "    os.open(target, os.O_WRONLY | os.O_TRUNC)\n"
        "    class Helper:\n"
        "        os.replace(tmp, path)\n"
    )
    findings = _rule(src)
    assert [f.line for f in findings] == [3], findings


def test_p048_comprehension_publish_does_not_clear_an_outer_violator() -> None:
    """Comprehensions evaluate in a scope of their own, so a rename inside one
    is not the enclosing function's publish either."""
    src = (
        "import os\n"
        "def f(target, pairs):\n"
        "    os.open(target, os.O_WRONLY | os.O_TRUNC)\n"
        "    return [os.replace(a, b) for a, b in pairs]\n"
    )
    findings = _rule(src)
    assert [f.line for f in findings] == [3], findings


def test_p048_class_body_violator_is_still_cleared_by_an_enclosing_publish() -> None:
    """Clearance stays inherited inward, uniformly with defs and lambdas — the
    documented no-solver allowance, not something the scope widening changes."""
    src = (
        "import os\n"
        "def f(tmp, path, target):\n"
        "    class Helper:\n"
        "        os.open(target, os.O_WRONLY | os.O_TRUNC)\n"
        "    os.replace(tmp, path)\n"
    )
    assert _rule(src) == []


def test_p048_bare_class_body_violator_fires() -> None:
    """With no publish anywhere above it, an ``O_TRUNC`` open in a class body
    is graded like any other — the widening must not make class bodies
    invisible instead of merely non-publishing."""
    src = (
        "import os\n"
        "class Writer:\n"
        "    fd = os.open('artifact.json', os.O_WRONLY | os.O_TRUNC)\n"
    )
    findings = _rule(src)
    assert [f.line for f in findings] == [3], findings


def test_p048_comprehension_target_shadows_an_outer_tainted_name() -> None:
    """A comprehension binds its targets, exactly like a lambda's parameters.

    Without the shadow frame the module-level ``flags`` taint would reach in
    and make this a false positive.
    """
    src = (
        "import os\n"
        "flags = os.O_WRONLY | os.O_TRUNC\n"
        "fds = [os.open(p, flags) for p, flags in candidates]\n"
    )
    assert _rule(src) == []


def test_p048_first_iterable_is_not_shadowed_by_its_own_target() -> None:
    """Python evaluates a comprehension's FIRST iterable in the enclosing scope,
    before any target is bound — so a colliding target name must not shadow the
    outer taint there.

    Regression: adding comprehension targets to the shadow frame wholesale made
    this a false negative. The `os.open` reads the module's tainted `flags`,
    not the `flags` the comprehension goes on to bind.
    """
    src = (
        "import os\n"
        "flags = os.O_WRONLY | os.O_TRUNC\n"
        "r = [x for flags in [os.open('artifact.json', flags)] for x in [0]]\n"
    )
    findings = _rule(src)
    assert [f.line for f in findings] == [3], findings


def test_p048_later_iterable_is_shadowed_by_an_earlier_target() -> None:
    """The mirror case: a *second* generator's iterable evaluates inside the
    comprehension, with earlier targets bound, so the shadow does apply."""
    src = (
        "import os\n"
        "flags = os.O_WRONLY | os.O_TRUNC\n"
        "r = [x for flags in candidates for x in [os.open('a', flags)]]\n"
    )
    assert _rule(src) == []


def test_p048_publish_in_a_first_iterable_clears_an_enclosing_violator() -> None:
    """Own-level follows the same split: a rename in the first iterable runs in
    the enclosing function, so it is that function's publish."""
    src = (
        "import os\n"
        "def f(target, tmp, path, xs):\n"
        "    os.open(target, os.O_WRONLY | os.O_TRUNC)\n"
        "    return [x for x in [os.replace(tmp, path)]]\n"
    )
    assert _rule(src) == []


def test_p048_genexp_first_iterable_behaves_like_a_listcomp() -> None:
    """All four forms share the helper, so the fix must not be ListComp-only."""
    src = (
        "import os\n"
        "flags = os.O_WRONLY | os.O_TRUNC\n"
        "g = (x for flags in [os.open('artifact.json', flags)] for x in [0])\n"
    )
    findings = _rule(src)
    assert [f.line for f in findings] == [3], findings


def test_p048_passes_without_o_trunc() -> None:
    src = "import os\nfd = os.open('x', os.O_WRONLY | os.O_CREAT, 0o600)\n"
    assert _rule(src) == []


def test_p048_ignores_the_builtin_open() -> None:
    """The builtin returns a file object, not a descriptor — different class."""
    src = "fh = open('x', 'wb')\nfh.write(b'x')\n"
    assert _rule(src) == []


def test_p048_ignores_o_trunc_outside_os_open() -> None:
    src = "import os\nFLAGS = os.O_WRONLY | os.O_TRUNC\n"
    assert _rule(src) == []


# ── Inline suppression ───────────────────────────────────────────────────────


def test_p048_inline_ignore_suppresses() -> None:
    src = (
        "import os\n"
        "def dump(path):\n"
        "    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)  # conformance: ignore[P050] single-consumer diagnostic\n"
    )
    (finding,) = _rule(src)
    assert finding.suppressed
    assert finding.suppression_justification == "single-consumer diagnostic"


def test_p048_ignore_on_line_above_suppresses() -> None:
    src = (
        "import os\n"
        "def dump(path):\n"
        "    # conformance: ignore[P050] single-consumer diagnostic\n"
        "    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)\n"
    )
    (finding,) = _rule(src)
    assert finding.suppressed
