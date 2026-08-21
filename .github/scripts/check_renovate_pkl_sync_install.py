#!/usr/bin/env python3
"""Guard: every sibling module renovate_pkl_sync.py imports must be installed
onto PATH by the fleet runner's install step in .github/workflows/renovate.yaml.

That step copies a single file to /usr/local/bin/renovate-pkl-sync with `sudo
install`, which is what makes this failure mode possible: the retired
renovate-pkl-sync.yaml reusable sparse-checked out the whole .github/scripts/
directory and so carried every sibling for free. renovate_pkl_sync.py
resolves its sibling imports via `sys.path.insert(0, str(Path(__file__).parent))`,
so any local module it imports must be copied alongside it in that same step —
otherwise the bare PATH command raises ModuleNotFoundError at runtime for every
consumer repo, every run (see FND-<ticket>, contract-toolkit 0.20.0 rollout).

Run standalone (`python3 check_renovate_pkl_sync_install.py`) or via the tested
functions in this module (`.github/scripts/tests/test_check_renovate_pkl_sync_install.py`).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
DRIVER = SCRIPTS_DIR / "renovate_pkl_sync.py"
WORKFLOW = SCRIPTS_DIR.parent / "workflows" / "renovate.yaml"

_INSTALL_RE = re.compile(r"install\s+-m\s+\d+\s+(\S+)\s+/usr/local/bin/\S+")


def local_sibling_imports(driver_source: str) -> set[str]:
    """Top-level module names the driver imports that resolve to a local .py file.

    Collects both `from X import ...` and plain/aliased `import X [as Y]` —
    either form needs X.py co-installed on PATH. The local-file existence check
    is what excludes stdlib (`import os`/`import sys` have no sibling .py).
    """
    tree = ast.parse(driver_source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
    return {name for name in imported if (SCRIPTS_DIR / f"{name}.py").exists()}


def installed_module_names(workflow_text: str) -> set[str]:
    """Module names (stem) copied onto /usr/local/bin by the install step."""
    return {Path(src).stem for src in _INSTALL_RE.findall(workflow_text)}


def missing_installs(driver_source: str, workflow_text: str) -> set[str]:
    """Sibling imports the driver needs that the install step never copies."""
    return local_sibling_imports(driver_source) - installed_module_names(workflow_text)


def main() -> int:
    missing = missing_installs(DRIVER.read_text(), WORKFLOW.read_text())
    if missing:
        print(
            "renovate.yaml's 'Install pkl-sync driver on PATH' step does not "
            f"install these sibling modules that renovate_pkl_sync.py imports: "
            f"{sorted(missing)}. Add a `sudo install` line for each into "
            "/usr/local/bin alongside renovate-pkl-sync, or the bare PATH "
            "command will ModuleNotFoundError at runtime.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
