"""Resolve an import to a source file inside the scanned repository.

A check that follows a name into the module it was imported from (E004/E021
tracing an error class's ancestry, P023 following a call into a sync helper)
needs the same answer: which ``.py`` file under the repo root does
``from <module> import x`` load? Anything outside the root — a third-party
package, the standard library — resolves to ``None``, so a check never reads
code the scanned repo does not own.
"""

from __future__ import annotations

from pathlib import Path


def repo_module_file(
    root: Path | None, module: str | None, level: int, current: Path | None
) -> Path | None:
    """The file ``from <module> import …`` loads, if it lives under *root*.

    *level* is the import's leading-dot count; a relative import resolves
    against *current*, the importing file. An absolute import resolves against
    *root*, the layout every scanned repo uses (``app/…``, ``application_sdk/…``).
    """
    if root is None:
        return None
    root = root.resolve()
    if level:
        if current is None:
            return None
        base = current.resolve().parent
        for _ in range(level - 1):
            base = base.parent
    else:
        base = root
    parts = module.split(".") if module else []
    for candidate in (
        base.joinpath(*parts).with_suffix(".py") if parts else None,
        base.joinpath(*parts, "__init__.py"),
    ):
        if (
            candidate is not None
            and candidate.is_file()
            and candidate.resolve().is_relative_to(root)
        ):
            return candidate.resolve()
    return None
