"""The remediate skill template renders to its own bytes.

The SDK repo's ``.claude/skills/remediate/SKILL.md`` is a symlink to
``conformance/bootstrap/templates/remediate.md``, so the repo's /remediate
and the file ``bootstrap`` writes into connector apps are one source. That
only holds while rendering is the identity: a template variable or block
added here would show up raw in the repo's skill. If one is ever needed,
generate the repo's skill from ``render`` instead of symlinking it.
"""

from __future__ import annotations

from importlib.resources import files

from conformance.bootstrap.render import render


def test_remediate_template_renders_verbatim() -> None:
    raw = (
        files("conformance.bootstrap")
        .joinpath("templates/remediate.md")
        .read_text(encoding="utf-8")
    )
    assert render("remediate.md") == raw
