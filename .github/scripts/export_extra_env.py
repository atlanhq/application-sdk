"""Export a caller-supplied JSON env map to ``$GITHUB_ENV``.

Why this exists
---------------
``tests-reusable.yaml``'s e2e job owns the tenant-side secrets (SDR_TEST_TENANT,
SDR_CLIENT_ID/SECRET, ATLAN_API_KEY), but the *source* credentials a connector
crawls with are per-connector and unknowable to the reusable: snowflake needs
``SNOWFLAKE_E2E_ACCOUNT/USER/PRIVATE_KEY``, saphana needs
``E2E_SAPHANA_HOST/PORT/...``, glue needs AWS keys. The ``sdr-e2e`` action
documents these as the caller's responsibility — which, for a repo on the thin
caller, means they have to arrive through a reusable-workflow input.

A ``KEY=VALUE``-per-line input cannot carry them: a Snowflake key-pair secret is
a multi-line PEM, and ``$GITHUB_ENV`` needs the heredoc form for any value
containing a newline. So the input is a JSON object instead, which the caller
builds with ``toJSON(secrets.X)`` so escaping is GitHub's job, and this script
re-emits it in the heredoc form.

Values are registered secrets in the calling job (``secrets: inherit``), so the
runner's log masking still applies to anything echoed downstream.

Usage::

    python export_extra_env.py --json "$EXTRA_ENV" >> "$GITHUB_ENV"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid

# A whitelist, not a blacklist of hostile characters. The runner splits a
# ``$GITHUB_ENV`` heredoc header on the FIRST ``<<``, so a name containing
# ``<<`` would leave it hunting for a delimiter the closing line never matches
# ("Invalid value. Matching delimiter not found"). Enumerating characters to
# reject keeps missing cases like that one, so only POSIX-shaped names pass.
# Matched with ``fullmatch``: an anchored ``$`` would still admit a trailing
# newline.
_VALID_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ExtraEnvError(ValueError):
    """The caller-supplied extra-env payload is not usable."""


def render(payload: str) -> str:
    """Return ``$GITHUB_ENV`` lines for the JSON object in *payload*.

    Every value is emitted in the heredoc form, not ``KEY=VALUE``: it is correct
    for single-line values too, so there is no branch that only multi-line
    values exercise. The delimiter embeds a random token so a value that itself
    contains the delimiter cannot terminate the block early — the documented
    injection concern for ``$GITHUB_ENV``.
    """
    payload = payload.strip()
    if not payload:
        return ""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ExtraEnvError(
            f"extra-env is not valid JSON ({exc}). Build it with "
            'toJSON(secrets.NAME) per value, e.g. {"E2E_HOST": ${{ '
            "toJSON(secrets.E2E_HOST) }}}."
        ) from exc
    if not isinstance(parsed, dict):
        raise ExtraEnvError(
            f"extra-env must be a JSON object, got {type(parsed).__name__}."
        )

    lines: list[str] = []
    for name, value in parsed.items():
        if not name or not isinstance(name, str):
            raise ExtraEnvError(f"extra-env keys must be non-empty strings: {name!r}")
        if not _VALID_NAME.fullmatch(name):
            raise ExtraEnvError(
                f"extra-env key {name!r} is not a valid environment variable "
                "name (expected ^[A-Za-z_][A-Za-z0-9_]*$)."
            )
        if value is None:
            # A caller referencing an unset secret gets an empty string from
            # GitHub, but tolerate an explicit null the same way rather than
            # writing the literal "None".
            value = ""
        if not isinstance(value, (str, int, float, bool)):
            raise ExtraEnvError(
                f"extra-env value for {name!r} must be a scalar, got "
                f"{type(value).__name__}."
            )
        text = str(value)
        delimiter = f"__EXTRA_ENV_{uuid.uuid4().hex}__"
        while delimiter in text:  # pragma: no cover - uuid collision guard
            delimiter = f"__EXTRA_ENV_{uuid.uuid4().hex}__"
        lines.append(f"{name}<<{delimiter}\n{text}\n{delimiter}")
    return "\n".join(lines) + "\n" if lines else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        default="",
        help="JSON object of environment variables. Empty is a no-op.",
    )
    args = parser.parse_args()
    try:
        sys.stdout.write(render(args.json))
    except ExtraEnvError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
