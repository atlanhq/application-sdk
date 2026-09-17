"""The phase job must install what the phase scripts import.

`sdk-loop-phase.yml` runs `.github/scripts/sdk_loop_phase.py` under the
runner's `python3`, not under `uv run`. CI runs the same scripts' tests under
`uv run pytest`, which has every project dependency. So a third-party import
added to a loop script is green in CI and `ModuleNotFoundError` in production —
which is exactly what happened when the verdict renderer landed with
`import yaml`: every `@sdk-loop` review failed at import for a day and nothing
went red anywhere.

This test derives the third-party import set from the scripts themselves and
asserts the workflow installs each one. A hand-maintained list here would be
updated in the same commit that forgets the workflow.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPTS = REPO / ".github/scripts"
WORKFLOW = REPO / ".github/workflows/sdk-loop-phase.yml"

#: Every module the phase job may end up importing. `sdk_review_*` because
#: `sdk_loop_prep` imports `sdk_review_approve` and the phase imports the
#: prep module.
PATTERNS = ("sdk_loop*.py", "sdk_review*.py")


def _third_party_imports() -> dict[str, set[str]]:
    stdlib = set(sys.stdlib_module_names)
    local = {p.stem for p in SCRIPTS.glob("*.py")}
    found: dict[str, set[str]] = {}
    for pattern in PATTERNS:
        for path in sorted(SCRIPTS.glob(pattern)):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif (
                    isinstance(node, ast.ImportFrom) and node.module and node.level == 0
                ):
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name not in stdlib and name not in local:
                        found.setdefault(name, set()).add(path.name)
    return found


#: Import name -> the distribution the workflow has to install.
DIST_FOR = {"yaml": "pyyaml"}


def test_the_phase_job_installs_what_the_scripts_import() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8").lower()
    install_lines = [ln for ln in workflow.splitlines() if "pip install" in ln]
    assert install_lines, "sdk-loop-phase.yml has no pip install step at all"
    installed = " ".join(install_lines)

    missing = []
    for module, files in sorted(_third_party_imports().items()):
        dist = DIST_FOR.get(module, module).lower()
        if dist not in installed:
            missing.append(
                f"{module} ({dist}) — imported by {', '.join(sorted(files))}"
            )
    assert not missing, (
        "the phase job would fail on import in production:\n  "
        + "\n  ".join(missing)
        + "\nAdd the distribution to the `pip install` step in sdk-loop-phase.yml."
    )


def test_every_third_party_import_has_a_known_distribution() -> None:
    """An import name that is not its distribution name (`yaml` → `pyyaml`)
    needs the mapping, or the test above compares the wrong string."""
    unmapped = sorted(
        m for m in _third_party_imports() if m not in DIST_FOR and m != m.lower()
    )
    assert not unmapped, f"add these to DIST_FOR: {unmapped}"
