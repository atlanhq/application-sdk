"""Class-definition checks for an App's configuration ``ClassVar`` settings.

Every setting below is read long after the class is defined — by the worker at
boot, by an interceptor, or inside a running task — and each reader coerces a
bad value instead of failing: a mistyped posture resolves to soft, a number out
of range is clamped, a bare string becomes a set of letters. That is deliberate
(a typo must never abort a run), but it makes the typo invisible. These checks
move the complaint to import, where the author sees it.

Each problem is a ``DeprecationWarning`` plus a logged WARNING today, and
becomes an :class:`~application_sdk.app.entrypoint.EntryPointDeclarationError`
in :data:`APP_CONFIG_ERROR_VERSION`. The readers' coercion is unchanged.

Only a class's own declarations are checked, so a bad value is reported once,
at the class that wrote it, not again for every subclass that inherits it.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Mapping

from application_sdk.handler.contracts import PreflightGateMode
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: The release in which every warning here becomes an
#: ``EntryPointDeclarationError`` at class definition.
APP_CONFIG_ERROR_VERSION = "4.0.0"

#: An explicit ``App.name``: letters, digits, ``.``, ``_`` and ``-``, starting
#: with a letter or digit. It is a segment of the workflow type
#: (``{app}:{entry-point}``), the activity names and the task queue, so a
#: space or a ``:`` breaks routing. Uppercase is allowed: live apps declare
#: PascalCase names.
_APP_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_POSTURES = frozenset({"hard", "soft"})

#: ``warnings.warn`` stacklevel that lands on the ``class X(App):`` statement
#: for a direct ``App`` subclass: ``warn_config_problem`` →
#: ``validate_app_config`` → ``App.__init_subclass__`` → ``ABCMeta.__new__`` → the
#: class statement.
#: A template with its own ``__init_subclass__`` adds a frame; the logged line
#: names the class either way.
_STACKLEVEL = 5


def _check_posture(value: object) -> str:
    if isinstance(value, PreflightGateMode):
        return ""
    if isinstance(value, str) and value.strip().lower() in _POSTURES:
        return ""
    return (
        "is not 'hard' or 'soft'; it resolves to soft, so the setting never "
        "blocks — use 'hard' or 'soft'"
    )


def _check_bool(value: object) -> str:
    if isinstance(value, bool):
        return ""
    return "is not a bool; any truthy value (even 'no') turns it on — use True or False"


def _check_str(value: object) -> str:
    return "" if isinstance(value, str) else "is not a str"


def _check_name(value: object) -> str:
    if not isinstance(value, str):
        return "is not a str"
    if value == "" or _APP_NAME_RE.fullmatch(value):
        return ""
    return (
        "is not a valid app name; use letters, digits, '.', '_' and '-' only "
        "(it is part of the workflow type, activity names and task queue)"
    )


def _check_tags(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, Mapping):
        return "is not a dict[str, str]"
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        return "is not a dict[str, str]; every key and value must be a str"
    return ""


def _check_passthrough_modules(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, bytes)):
        return (
            "is a bare string, which is read as a set of its single characters; "
            "wrap it: {'module'}"
        )
    if not isinstance(value, (set, frozenset, list, tuple)):
        return "is not a set of module names"
    if not all(isinstance(m, str) and m for m in value):
        return "is not a set of module names; every element must be a non-empty str"
    return ""


def _int_problem(value: object, clamp: Callable[[object], tuple[int, str]]) -> str:
    # bool is an int subclass; True is a typo for a number here, not 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return "is not an int"
    _, complaint = clamp(value)
    return f"is out of range ({complaint}); the worker clamps it" if complaint else ""


def _check_gate_timeout(value: object) -> str:
    from application_sdk.execution._temporal.preflight_gate import (  # noqa: PLC0415 — only when the setting is declared; keeps App import light
        gate_budget_seconds,
    )

    return _int_problem(value, gate_budget_seconds)


def _check_gate_attempts(value: object) -> str:
    from application_sdk.execution._temporal.preflight_gate import (  # noqa: PLC0415 — only when the setting is declared; keeps App import light
        gate_attempts,
    )

    return _int_problem(value, gate_attempts)


def _check_warmup_ceiling(value: object) -> str:
    from application_sdk.handler._warmup import (  # noqa: PLC0415 — only when the setting is declared
        warmup_ceiling_seconds,
    )

    return _int_problem(value, warmup_ceiling_seconds)


_CHECKS: tuple[tuple[str, Callable[[object], str]], ...] = (
    ("name", _check_name),
    ("version", _check_str),
    ("description", _check_str),
    ("tags", _check_tags),
    ("passthrough_modules", _check_passthrough_modules),
    ("preflight_gate_mode", _check_posture),
    ("artifact_validation_mode", _check_posture),
    ("preflight_gate_timeout_seconds", _check_gate_timeout),
    ("preflight_gate_max_attempts", _check_gate_attempts),
    ("preflight_warmup_ceiling_seconds", _check_warmup_ceiling),
    ("preflight_verify_storage", _check_bool),
)


def _check_warmup_probe_timeout(cls: type) -> str:
    """The probe timeout is bounded by the *effective* ceiling, so it is
    re-checked when either of the two is declared on ``cls``."""
    from application_sdk.handler._warmup import (  # noqa: PLC0415 — only when the setting is declared
        warmup_ceiling_seconds,
        warmup_probe_timeout_seconds,
    )

    ceiling, _ = warmup_ceiling_seconds(
        getattr(cls, "preflight_warmup_ceiling_seconds", None)
    )
    return _int_problem(
        getattr(cls, "preflight_warmup_probe_timeout_seconds", None),
        lambda raw: warmup_probe_timeout_seconds(raw, ceiling),
    )


def config_problems(cls: type) -> list[tuple[str, object, str]]:
    """``(attribute, value, problem)`` for each bad setting ``cls`` declares itself."""
    own = cls.__dict__
    problems = [
        (attr, own[attr], problem)
        for attr, check in _CHECKS
        if attr in own and (problem := check(own[attr]))
    ]
    probe = "preflight_warmup_probe_timeout_seconds"
    if probe in own or "preflight_warmup_ceiling_seconds" in own:
        problem = _check_warmup_probe_timeout(cls)
        if problem:
            problems.append((probe, getattr(cls, probe, None), problem))
    return problems


def warn_config_problem(cls: type, attr: str, value: object, problem: str) -> None:
    """Emit one class-definition complaint as a warning and a log line."""
    message = (
        f"{cls.__module__}.{cls.__qualname__}: {attr} = {value!r} {problem}. "
        f"This is a warning today and will raise EntryPointDeclarationError "
        f"in v{APP_CONFIG_ERROR_VERSION}."
    )
    warnings.warn(message, DeprecationWarning, stacklevel=_STACKLEVEL)
    logger.warning("%s", message)


def validate_app_config(cls: type) -> None:
    """Warn about each configuration ``ClassVar`` ``cls`` declares with a bad value.

    Never raises. The caller skips it inside the workflow sandbox, which
    re-imports app modules on every run (the worker already reported it at
    boot); the check lives there so this module stays free of Temporal.
    """
    for attr, value, problem in config_problems(cls):
        warn_config_problem(cls, attr, value, problem)
