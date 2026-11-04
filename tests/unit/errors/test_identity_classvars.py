"""Every ``AppError`` leaf keeps its routing identity on the class.

ADR 0013 makes ``category``, ``code``, ``audience`` and ``default_retryable``
the per-class identity of a typed error: one code always means the same
category, the same owner and the same retry default. Consumers depend on that
in two ways. Some read the identity off the class (``type(exc).audience`` in
the Temporal log interceptor and the preflight outcome row); alerting, pattern
pipelines and RCA tooling key on ``code`` + ``audience`` and assume a code
never changes owner.

Turning one of these into a dataclass field, an instance assignment or a
property lets a single code route differently per instance, and every
class-level reader then reports the stale value. When a failure's owner
depends on where it happened (deployment mode, locus), raise a subclass that
fixes the other value as a ClassVar — ADR 0013: "Subclasses override
``audience`` when the locus is known."
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import textwrap
from pathlib import Path

import pytest

import application_sdk
from application_sdk.errors.base import AppError
from application_sdk.errors.categories import Audience, FailureCategory

_ROOT = Path(application_sdk.__file__).parent

# The identity attributes and the type each must resolve to on the class.
_IDENTITY: dict[str, type] = {
    "category": FailureCategory,
    "code": str,
    "audience": Audience,
    "default_retryable": bool,
}

# Wire-reconstruction leaves only. ``_EnvelopeError`` stands in for a code this
# consumer has no leaf for, so its routing has to come from the envelope it was
# rebuilt from. That is not a precedent for a leaf the SDK raises itself.
_RECONSTRUCTION_LEAVES: frozenset[str] = frozenset(
    {"application_sdk.templates.sql_app._EnvelopeError"}
)


def _import_all_sdk_modules() -> None:
    # File-based, as in tests/unit/test_imports.py, so a broken package
    # __init__ cannot silently hide the leaves under it. A missing optional
    # dependency is tolerated here (test_imports owns import health); the
    # anchor assertion below fails loudly if that drops a known leaf.
    for path in sorted(_ROOT.rglob("*.py")):
        rel = path.relative_to(_ROOT)
        parts = (
            rel.parent.parts
            if path.name == "__init__.py"
            else rel.with_suffix("").parts
        )
        if not parts:
            continue
        try:
            importlib.import_module("application_sdk." + ".".join(parts))
        except ModuleNotFoundError:
            continue


def _sdk_leaves() -> list[type[AppError]]:
    _import_all_sdk_modules()
    seen: set[type[AppError]] = set()
    stack: list[type[AppError]] = [AppError]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub not in seen:
                seen.add(sub)
                stack.append(sub)
    # Test modules define throwaway subclasses; only the SDK's own are in scope.
    return sorted(
        (c for c in seen if c.__module__.startswith("application_sdk.")),
        key=lambda c: f"{c.__module__}.{c.__qualname__}",
    )


_LEAVES = _sdk_leaves()


def _qualified(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _instance_writes(cls: type) -> list[str]:
    """Writes to an identity attribute on ``self`` inside the class body.

    The class-level check cannot see ``self.audience = ...`` in
    ``__post_init__`` when no field is declared: the class still resolves to
    the ClassVar while every instance routes elsewhere.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    found: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr in _IDENTITY
            ):
                found.append(
                    f"assigns `self.{target.attr}` (line {node.lineno} of the class)"
                )
        if isinstance(node, ast.Call):
            # setattr(self, "audience", v) / object.__setattr__(self, "audience", v)
            # / self.__setattr__("audience", v): the name is the first string arg.
            is_setattr = (
                isinstance(node.func, ast.Name) and node.func.id == "setattr"
            ) or (
                isinstance(node.func, ast.Attribute) and node.func.attr == "__setattr__"
            )
            names = [
                a.value
                for a in node.args[:2]
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            if is_setattr and names and names[0] in _IDENTITY:
                found.append(
                    f"sets `{names[0]}` via setattr (line {node.lineno} of the class)"
                )
    return found


def test_discovery_finds_the_known_leaves() -> None:
    names = {_qualified(c) for c in _LEAVES}
    for anchor in (
        "application_sdk.errors.leaves.DiskFullError",
        "application_sdk.storage.errors.StorageDiskFullError",
        *_RECONSTRUCTION_LEAVES,
    ):
        assert anchor in names, (
            f"discovery missed {anchor}; the guard would pass vacuously"
        )


@pytest.mark.parametrize("cls", _LEAVES, ids=_qualified)
def test_identity_is_a_classvar(cls: type[AppError]) -> None:
    if _qualified(cls) in _RECONSTRUCTION_LEAVES:
        pytest.skip("wire-reconstruction leaf: routing comes from the envelope")
    fields = (
        {f.name for f in dataclasses.fields(cls)}
        if dataclasses.is_dataclass(cls)
        else set()
    )
    problems = []
    for name, expected in _IDENTITY.items():
        if name in fields:
            problems.append(f"`{name}` is a dataclass field")
            continue
        static = inspect.getattr_static(cls, name)
        if not isinstance(static, expected):
            problems.append(
                f"`{name}` resolves to {type(static).__name__}, not a {expected.__name__} ClassVar"
            )
    problems.extend(_instance_writes(cls))
    assert not problems, (
        f"{_qualified(cls)}: {'; '.join(problems)}. category/code/audience/"
        "default_retryable are per-class identity (docs/adr/0013-error-hierarchy-"
        "and-failure-taxonomy.md). If the owner or category depends on where the "
        "failure happened, define a subclass that sets it as a ClassVar and raise "
        "that subclass at the point that knows the context. Do not vary it per "
        "instance: class-level readers such as type(exc).audience will report "
        "the class value."
    )
