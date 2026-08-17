"""Print the cloud KEYS that ``E2E_TENANT_MATRIX_JSON`` carries, and nothing else.

Why this exists (FND-354)
-------------------------
Removing a cloud from the tenant matrix secret is the only lever for taking a
cloud out of the e2e rotation that is fleet-wide, needs no connector PR, and
takes effect on the next run — which is exactly what an operator reaches for
when that cloud's tenant is down mid-incident. It used to do the opposite:
``discover_e2e_suites.py`` never saw the secret, so the defaulted fan-out still
emitted a leg for the removed cloud and ``resolve_e2e_tenant.py`` hard-failed it
in *every* e2e-running repo.

Teaching discovery to narrow the defaulted list needs it to see which clouds the
secret holds. This script is the bridge, and it is deliberately the narrowest
one that works: the payload comes in, one comma-separated list of **key names**
goes out::

    python3 e2e_tenant_matrix_clouds.py --matrix-json "$E2E_TENANT_MATRIX_JSON" \
      >> "$GITHUB_OUTPUT"        # clouds=aws,gcp

The credentials never leave this process. Discovery is handed the key list, not
the blob, so the only script that can see a tenant credential remains
``resolve_e2e_tenant.py`` — and it renders exactly one cloud's entry per leg.

Keys, not validity
------------------
A key counts as available because it is present, not because its entry is
well-formed. A cloud whose entry is missing ``client_secret`` is a coverage hole
that must stay red, and ``resolve_e2e_tenant.py`` already reports it precisely
per leg; silently narrowing it away here would convert that red into a run that
looks complete and is not.

Degradation
-----------
An unusable payload prints an empty list and warns, rather than failing. Empty
means "not known", discovery then narrows nothing, and the run behaves exactly
as it did before FND-354 — including whatever error ``resolve_e2e_tenant.py``
raises per leg, which is the one that can name the actual defect in the secret.
Failing here instead would replace that precise per-leg diagnosis with a
discovery-time failure that knows strictly less.
"""

from __future__ import annotations

import argparse
import json
import sys


class MatrixCloudsError(ValueError):
    """The tenant matrix payload cannot be read as a cloud-keyed object."""


def cloud_keys(matrix_json: str) -> list[str]:
    """Return the sorted cloud keys in *matrix_json*.

    An empty payload yields no keys: the secret is not shared with this repo,
    and the caller already treats that as "no cloud dimension at all".

    Sorted rather than insertion-ordered because nothing downstream takes its
    leg order from here — discovery orders by :data:`DEFAULT_CLOUDS` — so a
    stable order is worth more than the secret author's.
    """
    payload = matrix_json.strip()
    if not payload:
        return []

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MatrixCloudsError(
            f"E2E_TENANT_MATRIX_JSON is not valid JSON ({exc})."
        ) from exc
    if not isinstance(parsed, dict):
        raise MatrixCloudsError(
            "E2E_TENANT_MATRIX_JSON must be a JSON object keyed by cloud, got "
            f"{type(parsed).__name__}."
        )

    # str() rather than a type assertion: JSON object keys are always strings.
    return sorted(str(key).strip() for key in parsed if str(key).strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--matrix-json",
        default="",
        help=(
            "E2E_TENANT_MATRIX_JSON: cloud -> credential map. Only its keys are "
            "read, and only its keys are ever printed."
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        clouds = cloud_keys(args.matrix_json)
    except MatrixCloudsError as exc:
        # Never echo the payload, not even a fragment of it: the JSONDecodeError
        # text carries a position, not content, and that is all that is quoted.
        print(
            f"::warning::{exc} The e2e cloud fan-out cannot be narrowed to what "
            "the secret carries, so it falls back to the SDK's full default "
            "list. A cloud the secret does not carry will fail its leg with the "
            "per-leg resolver's message, which names the actual defect.",
            file=sys.stderr,
        )
        clouds = []

    print(
        f"Tenant matrix carries {len(clouds)} cloud(s): "
        f"{', '.join(clouds) or 'none'}",
        file=sys.stderr,
    )
    print(f"clouds={','.join(clouds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
