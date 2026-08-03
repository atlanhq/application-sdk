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

Why the values must be masked explicitly
----------------------------------------
The caller registers the *composed blob* as a secret (``E2E_SOURCE_ENV_JSON``),
and it is easy to assume that covers the values inside it. It does not. The
runner's log masker replaces occurrences of each **registered string**; it does
not match substrings of one. So the blob is redacted, every individual value
extracted from it is not, and the first later step that renders an ``env:``
group prints each source credential in cleartext.

Registering them requires the ``::add-mask::`` workflow command, which the
runner only reads from a step's **stdout**. But this script's stdout is
redirected into ``$GITHUB_ENV`` by its callers, so mask commands written there
would land in the env file as garbage instead of reaching the runner. The two
streams are therefore separated into two modes, and callers invoke both — masks
first, so nothing is ever written to ``$GITHUB_ENV`` before it is redactable::

    python export_extra_env.py --json "$EXTRA_ENV" --mask-only
    python export_extra_env.py --json "$EXTRA_ENV" >> "$GITHUB_ENV"

``--mask-only`` is deliberately not merged into the default mode: the ordering
guarantee comes from the mask invocation being a separate, earlier command under
``bash -e``, and ``test_every_env_write_call_site_masks_first`` asserts no call
site writes ``$GITHUB_ENV`` without it.
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


def parse(payload: str) -> list[tuple[str, str]]:
    """Return validated ``(name, text)`` pairs for the JSON object in *payload*.

    Shared by both modes so ``--mask-only`` and the env write agree on exactly
    which values exist: a value the mask pass skipped but the env pass wrote
    would be an unredacted credential. Validation errors therefore surface from
    the mask pass too, which runs first — so a bad payload fails the step before
    anything reaches ``$GITHUB_ENV``.
    """
    payload = payload.strip()
    if not payload:
        return []
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

    pairs: list[tuple[str, str]] = []
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
            # Deliberately names the key and the type only. Never the value:
            # this string is printed to the log, and the value is a credential.
            raise ExtraEnvError(
                f"extra-env value for {name!r} must be a scalar, got "
                f"{type(value).__name__}."
            )
        pairs.append((name, str(value)))
    return pairs


def render(payload: str) -> str:
    """Return ``$GITHUB_ENV`` lines for the JSON object in *payload*.

    Every value is emitted in the heredoc form, not ``KEY=VALUE``: it is correct
    for single-line values too, so there is no branch that only multi-line
    values exercise. The delimiter embeds a random token so a value that itself
    contains the delimiter cannot terminate the block early — the documented
    injection concern for ``$GITHUB_ENV``.
    """
    lines: list[str] = []
    for name, text in parse(payload):
        delimiter = f"__EXTRA_ENV_{uuid.uuid4().hex}__"
        while delimiter in text:  # pragma: no cover - uuid collision guard
            delimiter = f"__EXTRA_ENV_{uuid.uuid4().hex}__"
        lines.append(f"{name}<<{delimiter}\n{text}\n{delimiter}")
    return "\n".join(lines) + "\n" if lines else ""


def escape(text: str) -> str:
    """Encode *text* as workflow-command data.

    The same transform ``@actions/core``'s ``setSecret`` applies before writing
    ``::add-mask::``, and the inverse of the runner's ``UnescapeData``: ``%``
    first (so the markers introduced next are not themselves re-encoded), then
    the line terminators that would otherwise split one command into several.
    Without the ``%`` step a value containing the literal text ``%0A`` would be
    unescaped by the runner into a newline and the wrong string registered.
    """
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def mask_values(text: str) -> list[str]:
    """Return the strings to register as secrets so *text* is redacted in logs.

    ``::add-mask::`` registers one exact string, and the runner's masker
    (``SecretMasker.ReplaceSecrets``) rewrites log output a line at a time. For a
    single-line value the whole value is therefore all that is needed. A
    multi-line value — a Snowflake key-pair PEM, a BigQuery service-account JSON
    — is different: no single log line ever contains the whole thing, so a
    registration of only the whole value matches nothing when the value is
    echoed. Its lines have to be registered in their own right.

    The whole value is registered as well as the lines. It costs one command and
    covers the renderings where the value does appear intact.

    Why this is done here rather than left to the runner: current
    ``AddMaskCommandExtension`` versions already split ``command.Data`` on
    ``\\r``/``\\n`` (``RemoveEmptyEntries | TrimEntries``) and register each
    piece, so on github.com-hosted runners the whole-value command alone would
    in fact be enough today. That split is a runner implementation detail, not
    part of the documented ``add-mask`` contract, and older self-hosted and GHES
    runners predate it (and predate ``TrimEntries``). Emitting the lines
    explicitly costs one command each and makes the outcome independent of the
    runner version. The stripped form is included for the same reason: it is what
    ``TrimEntries`` would have registered, and it is the form that matches when a
    padded value (an indented line of pretty-printed JSON, or a secret pasted
    with trailing whitespace) is re-emitted unpadded. It is registered for
    single-line values too, not just for the lines of a multi-line one — the
    padded and unpadded forms of ``" tok3n "`` are as distinct to the masker as
    any other two strings, and relying on the runner to trim it would reintroduce
    exactly the version dependence the per-line commands remove.

    Lines are split on ``\\r\\n``/``\\r``/``\\n`` only, matching what the runner
    treats as a line boundary. ``str.splitlines()`` is deliberately not used: it
    also breaks on ``\\v``, ``\\f``, ``\\x1c``-``\\x1e``, ``\\x85``, U+2028 and
    U+2029, so a value containing one of those would be registered as short
    fragments that redact unrelated log text.

    Blank and whitespace-only entries are dropped. The runner answers
    ``::add-mask::`` with no usable data by warning and registering nothing
    ("Can't add secret mask for empty string in ##[add-mask] command"), and a
    mask of `` `` would in any case match every space in the log.

    The whole value is always the first candidate: callers rely on the
    whole-value command being emitted first.
    """
    candidates = [text, text.strip()]
    if "\n" in text or "\r" in text:
        for line in re.split(r"\r\n|\r|\n", text):
            candidates.append(line)
            candidates.append(line.strip())

    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.strip():
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def render_masks(payload: str) -> str:
    """Return ``::add-mask::`` lines for every value in *payload*.

    Every scalar is masked, including the ones that look harmless. A host or a
    port is not a credential, but the caller decides what goes into the map and
    this script cannot tell which keys hold secrets — so it does not guess.
    """
    lines: list[str] = []
    for _name, text in parse(payload):
        lines.extend(f"::add-mask::{escape(value)}" for value in mask_values(text))
    return "\n".join(lines) + "\n" if lines else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        default="",
        help="JSON object of environment variables. Empty is a no-op.",
    )
    parser.add_argument(
        "--mask-only",
        action="store_true",
        help=(
            "Print only ::add-mask:: commands for the values, and no "
            "$GITHUB_ENV lines. Run this first, with stdout going to the log "
            "so the runner reads the commands, before the env-writing "
            "invocation redirects stdout into $GITHUB_ENV."
        ),
    )
    args = parser.parse_args()
    try:
        render_fn = render_masks if args.mask_only else render
        sys.stdout.write(render_fn(args.json))
    except ExtraEnvError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
