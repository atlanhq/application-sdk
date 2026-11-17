"""Name helpers every AST series needs: the last name of a dotted reference,
a class's base names, and the SDK's class-name-to-app-name transform.

One copy each, so a fix to how a name is read lands in every rule at once.
"""

from __future__ import annotations

import ast
import re


def leaf_name(node: ast.AST | None) -> str | None:
    """Return the last name of a simple reference, or ``None``.

    ``foo`` gives ``"foo"`` and ``a.b.foo`` gives ``"foo"``. Anything else (a
    call, a subscript, ``None``) gives ``None``.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def base_names(node: ast.ClassDef) -> list[str]:
    """Return the last name of each simple base of *node*, in declaration order.

    ``class A(Base, mod.Other, Generic[T])`` gives ``["Base", "Other"]``: a
    subscripted or computed base has no simple name and is left out.
    """
    return [name for base in node.bases if (name := leaf_name(base)) is not None]


def pascal_to_kebab(name: str) -> str:
    """Convert a PascalCase class name to the kebab-case app name the SDK derives.

    Must stay character-for-character equal to
    ``application_sdk.app.base._pascal_to_kebab``: the result is compared with
    the app name the toolkit wrote into the generated manifest, so any
    difference reads one app as two. A parity test enforces this.

    Examples: ``MyAwesomeApp`` → ``my-awesome-app``, ``HTTPHandler`` →
    ``http-handler``, ``S3Loader`` → ``s3-loader``.
    """
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    spaced = re.sub(r"([a-z\d])([A-Z])", r"\1-\2", spaced)
    return spaced.lower()
