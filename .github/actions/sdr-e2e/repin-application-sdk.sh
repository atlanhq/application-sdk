#!/usr/bin/env bash
#
# Rewrite the [tool.uv.sources] atlan-application-sdk entry in the caller
# repo's pyproject.toml to point at a specific git ref. Invoked from
# action.yaml when `application-sdk-ref` is supplied — i.e. on apps-sdk
# cross-repo dispatches.
#
# Reads:  $SDK_REF (the commit / branch / tag to pin to)
# Writes: pyproject.toml in CWD (the caller workspace)
#
# Idempotent: re-running with the same SDK_REF is a no-op diff.

set -euo pipefail

if [ -z "${SDK_REF:-}" ]; then
  echo "::error::SDK_REF env var not set"
  exit 1
fi

if [ ! -f pyproject.toml ]; then
  echo "::error::pyproject.toml not found in $(pwd)"
  exit 1
fi

python3 - <<PYEOF
import os
import pathlib
import re

ref = os.environ["SDK_REF"]
path = pathlib.Path("pyproject.toml")
src = path.read_text()

new_line = (
    'atlan-application-sdk = { git = '
    f'"https://github.com/atlanhq/application-sdk", rev = "{ref}" }}'
)

# Match the entire atlan-application-sdk source pin (single-line, any
# combination of branch/rev/tag/path inside the braces). Anchored to a
# line start and runs to a closing brace + EOL.
pattern = re.compile(
    r'^atlan-application-sdk\s*=\s*\{[^}]*\}\s*$',
    flags=re.MULTILINE,
)
replaced, n = pattern.subn(new_line, src)

if n > 1:
    # More than one pin is ambiguous — fail loudly rather than rewriting
    # something the user did not intend.
    raise SystemExit(
        f"::error::Multiple atlan-application-sdk source pins matched ({n}); "
        "expected exactly one."
    )

if n == 0:
    # No existing active pin — consumer repo uses PyPI. Insert the git
    # override under an ACTIVE [tool.uv.sources] header. A COMMENTED header
    # (``# [tool.uv.sources]``) must NOT count: inserting the pin after it
    # leaves the entry outside any table, so uv rejects it ("unknown field
    # atlan-application-sdk") and SILENTLY resolves the released PyPI SDK
    # instead — the false-green this guard exists to prevent.
    header = re.compile(r"^\[tool\.uv\.sources\]\s*$", flags=re.MULTILINE)
    m = header.search(replaced)
    if m:
        replaced = replaced[: m.end()] + f"\n{new_line}" + replaced[m.end() :]
        print(f"Inserted source pin under existing [tool.uv.sources] for {ref}")
    else:
        replaced = replaced.rstrip() + f"\n\n[tool.uv.sources]\n{new_line}\n"
        print(f"No active [tool.uv.sources]; appended a new section for {ref}")

path.write_text(replaced)

# Verify the rewrite placed the pin correctly under [tool.uv.sources]. Never
# leave a corrupt/mis-placed pin that would silently fall back to the PyPI SDK
# — fail the run loudly instead. This guard must NOT degrade to a no-op on the
# interpreters it is most likely to run under (the pre-build repin can run on
# python < 3.11, where tomllib is absent), so fall back to tomli and then to a
# structural regex check rather than skipping.
parser = None
try:
    import tomllib as parser  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as parser  # backport for < 3.11
    except ModuleNotFoundError:
        parser = None

if parser is not None:
    try:
        parsed = parser.loads(replaced)
    except Exception as exc:
        raise SystemExit(f"::error::repin produced invalid pyproject.toml: {exc}")
    entry = (
        parsed.get("tool", {}).get("uv", {}).get("sources", {}).get(
            "atlan-application-sdk"
        )
    )
    if not (isinstance(entry, dict) and entry.get("rev") == ref):
        raise SystemExit(
            "::error::repin did not place atlan-application-sdk = "
            f'{{ rev = "{ref}" }} under [tool.uv.sources]; refusing to let the '
            "build silently resolve the released PyPI SDK."
        )
else:
    # No TOML parser available — do NOT skip the guard. Structurally verify the
    # injected pin sits under an ACTIVE [tool.uv.sources] header (the nearest
    # preceding table header is exactly that, uncommented).
    lines = replaced.splitlines()
    idx = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.lstrip().startswith("atlan-application-sdk") and ref in ln
        ),
        None,
    )
    if idx is None:
        raise SystemExit(
            "::error::repin: injected atlan-application-sdk pin not found after rewrite."
        )
    header = next(
        (ln.strip() for ln in reversed(lines[:idx]) if re.match(r"^\s*\[", ln)),
        None,
    )
    if header != "[tool.uv.sources]":
        raise SystemExit(
            "::error::repin: pin is not under an active [tool.uv.sources] header "
            f"(nearest table header: {header!r}); uv would reject it and silently "
            "resolve the released PyPI SDK."
        )
print(f"Re-pinned application-sdk to {ref}")
PYEOF
