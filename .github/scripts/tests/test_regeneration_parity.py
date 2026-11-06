"""Every SDK entry point regenerates a contract with the same function.

Renovate's toolkit sync, the freshness gate, pre-test regeneration and certify's
contract-drift check used to carry their own regeneration. Copies drift: on a
production app, Renovate's sync kept a stale file that a plain regeneration
rewrites, so a merged toolkit bump left the tree failing the publish gate.
``renovate_pkl_sync.regenerate_tree`` is now the only implementation (FND-4736).
These tests fail when a caller grows its own again.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import check_generated_freshness  # noqa: E402
import regenerate_contract  # noqa: E402
import renovate_pkl_sync  # noqa: E402

SCRIPTS = Path(__file__).parent.parent


def test_every_entry_point_imports_the_shared_regeneration() -> None:
    assert regenerate_contract.regenerate_tree is renovate_pkl_sync.regenerate_tree
    # Certify imports this same name from renovate_pkl_sync.
    assert check_generated_freshness.regenerate is renovate_pkl_sync.regenerate


def _calls_pkl_eval(path: Path) -> bool:
    """Whether a module builds a ``pkl eval`` command list itself."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            values = [
                elt.value
                for elt in node.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
            if values[:2] == ["pkl", "eval"] and "-m" in values:
                return True
    return False


def test_no_other_entry_point_evaluates_a_contract_itself() -> None:
    """Only the shared module may run ``pkl eval -m``. (``-x`` property probes,
    as in ``regenerate_contract.opted_out_root_files``, read a flag and write
    nothing, so they are not a second generator.)"""
    for name in ("regenerate_contract.py", "check_generated_freshness.py"):
        assert not _calls_pkl_eval(SCRIPTS / name), name
    assert _calls_pkl_eval(SCRIPTS / "renovate_pkl_sync.py")
