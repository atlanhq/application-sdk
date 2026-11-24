"""The one way a CI script writes a GitHub Actions step output or summary.

Before this module, about 45 functions each appended to ``$GITHUB_OUTPUT`` their
own way, and only a handful could write a multi-line value safely (FND-4924).
Use these instead of opening the file yourself:

* ``set_output(name, value)`` / ``set_outputs({...})`` append ``name=value``.
  A value containing a newline is written in heredoc form with a random
  delimiter, so it cannot end early or smuggle in a second output, whatever the
  value contains.
* ``append_summary(markdown)`` appends to ``$GITHUB_STEP_SUMMARY``.

Outside Actions (the variable unset or empty), both write to stdout instead, so
a local run shows what it would have set. Neither ever raises on a missing
variable.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from collections.abc import Mapping

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _format(name: str, value: str) -> str:
    if not _NAME.match(name):
        raise ValueError(f"invalid step output name: {name!r}")
    if "\n" not in value and "\r" not in value:
        return f"{name}={value}\n"
    delimiter = f"EOF_{secrets.token_hex(16)}"
    while delimiter in value:  # astronomically unlikely; loop for certainty
        delimiter = f"EOF_{secrets.token_hex(16)}"
    return f"{name}<<{delimiter}\n{value}\n{delimiter}\n"


def _append(variable: str, text: str) -> None:
    path = os.environ.get(variable)
    if not path:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def set_outputs(outputs: Mapping[str, str]) -> None:
    """Append each ``name -> value`` to ``$GITHUB_OUTPUT``, in order."""
    _append("GITHUB_OUTPUT", "".join(_format(k, v) for k, v in outputs.items()))


def set_output(name: str, value: str) -> None:
    """Append one step output to ``$GITHUB_OUTPUT``."""
    set_outputs({name: value})


def append_summary(markdown: str) -> None:
    """Append markdown to ``$GITHUB_STEP_SUMMARY``, ending it with a newline."""
    _append(
        "GITHUB_STEP_SUMMARY", markdown if markdown.endswith("\n") else markdown + "\n"
    )
