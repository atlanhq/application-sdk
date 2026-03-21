"""Shared helpers for codemod unit tests."""

from __future__ import annotations

import textwrap
from typing import Any

import libcst as cst

from tools.migrate_v3.codemods import BaseCodemod


def transform(
    source: str, codemod_cls: type[BaseCodemod], **kwargs: Any
) -> tuple[str, list[str]]:
    """Parse *source*, run *codemod_cls*, return (new_source, changes).

    Leading indentation is stripped via ``textwrap.dedent`` before parsing so
    test strings can be indented naturally in the source file.
    """
    tree = cst.parse_module(textwrap.dedent(source).strip() + "\n")
    codemod = codemod_cls(**kwargs)
    new_tree = codemod.transform(tree)
    return new_tree.code, codemod.changes
