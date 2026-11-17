#!/usr/bin/env bash
# Regenerate RSA key pair + JWKS for local Temporal JWT auth.
# Run from the repo root or from local/temporal-auth/.
#
# Usage: bash local/temporal-auth/scripts/gen_keys.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$AUTH_DIR/../.." && pwd)"

echo "Generating RSA 2048 private key..."
openssl genrsa -out "$AUTH_DIR/private.pem" 2048 2>/dev/null

echo "Extracting public key..."
openssl rsa -in "$AUTH_DIR/private.pem" -pubout -out "$AUTH_DIR/public.pem" 2>/dev/null

echo "Generating JWKS..."
mkdir -p "$AUTH_DIR/.well-known"
cd "$REPO_ROOT"
uv run python - <<'EOF'
import sys, json, base64
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

auth_dir = Path("local/temporal-auth")
pub = load_pem_public_key((auth_dir / "public.pem").read_bytes())
assert isinstance(pub, RSAPublicKey)
nums = pub.public_numbers()

def b64url(n):
    length = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

jwks = {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "temporal-local-dev",
                   "n": b64url(nums.n), "e": b64url(nums.e)}]}
(auth_dir / ".well-known" / "jwks.json").write_text(json.dumps(jwks, indent=2))
print(f"  Written: {auth_dir}/.well-known/jwks.json")
EOF

echo "Done. Files written:"
echo "  $AUTH_DIR/private.pem  (gitignored — keep safe)"
echo "  $AUTH_DIR/public.pem"
echo "  $AUTH_DIR/.well-known/jwks.json  (gitignored — regen with this script)"
