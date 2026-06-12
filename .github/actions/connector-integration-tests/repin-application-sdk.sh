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

if n == 0:
    # No existing [tool.uv.sources] entry — consumer repo uses PyPI.
    # Insert the git override under [tool.uv.sources], creating the
    # section if it doesn't exist.
    if "[tool.uv.sources]" in src:
        replaced = src.replace(
            "[tool.uv.sources]",
            f"[tool.uv.sources]\n{new_line}",
            1,
        )
    else:
        replaced = src + f"\n[tool.uv.sources]\n{new_line}\n"
    print(f"No existing source pin found; inserted new entry for {ref}")

if n > 1:
    # More than one pin is ambiguous — fail loudly rather than rewriting
    # something the user did not intend.
    raise SystemExit(
        f"Multiple atlan-application-sdk source pins matched ({n}); "
        "expected exactly one."
    )

path.write_text(replaced)
print(f"Re-pinned application-sdk to {ref}")
PYEOF
