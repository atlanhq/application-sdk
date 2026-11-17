#!/usr/bin/env python3
"""Generate a signed RS256 JWT for local Temporal auth testing.

The private key is read from local/temporal-auth/private.pem (relative to repo root,
or override with --key-file).

Token format matches Temporal's default ClaimMapper:
  permissions: ["<namespace>:<role>", ...]
  roles:        read | write | worker | admin
  system role:  temporal-system:admin

Examples
--------
# Admin token (1 hour) — use for namespace registration and CLI ops
python local/temporal-auth/scripts/gen_token.py \\
    --permissions temporal-system:admin

# Worker token (1 hour) — use for SDK connections
python local/temporal-auth/scripts/gen_token.py \\
    --permissions default:worker

# Short-lived worker token (5 seconds) — triggers auth expiry in tests
python local/temporal-auth/scripts/gen_token.py \\
    --ttl 5 --permissions default:worker

# Print as Bearer header value
python local/temporal-auth/scripts/gen_token.py \\
    --permissions default:worker --bearer
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Resolve repo root (two levels up from this script)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_KEY = _REPO_ROOT / "local" / "temporal-auth" / "private.pem"
_KID = "temporal-local-dev"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--permissions",
        nargs="+",
        default=["temporal-system:admin"],
        metavar="NS:ROLE",
        help='Permission claims, e.g. "default:worker" or "temporal-system:admin" (default: temporal-system:admin)',
    )
    p.add_argument(
        "--ttl",
        type=int,
        default=3600,
        help="Token lifetime in seconds (default: 3600). Use small values to test expiry.",
    )
    p.add_argument(
        "--subject",
        default="local-dev@temporal",
        help="JWT sub claim (default: local-dev@temporal)",
    )
    p.add_argument(
        "--key-file",
        type=Path,
        default=_DEFAULT_KEY,
        help=f"Path to RSA private key PEM (default: {_DEFAULT_KEY})",
    )
    p.add_argument(
        "--bearer",
        action="store_true",
        help='Prefix output with "Bearer " (for direct use in Authorization header)',
    )
    return p


def main() -> None:
    try:
        import jwt as _pyjwt  # noqa: F401
    except ImportError:  # conformance: ignore[E008] CLI script: print() + sys.exit(1) is intentional user-facing error reporting
        print(
            "ERROR: pyjwt not installed. Run: uv add pyjwt cryptography",
            file=sys.stderr,
        )
        sys.exit(1)

    import jwt

    args = _build_parser().parse_args()

    key_path: Path = args.key_file
    if not key_path.exists():
        print(f"ERROR: private key not found at {key_path}", file=sys.stderr)
        print(
            "Run: bash local/temporal-auth/scripts/gen_keys.sh",
            file=sys.stderr,
        )
        sys.exit(1)

    private_key = key_path.read_bytes()
    now = int(time.time())

    payload = {
        "iss": "temporal-local",
        "sub": args.subject,
        "iat": now,
        "exp": now + args.ttl,
        "permissions": args.permissions,
    }

    token: str = jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": _KID},
    )

    if args.bearer:
        print(f"Bearer {token}")
    else:
        print(token)


if __name__ == "__main__":
    main()
