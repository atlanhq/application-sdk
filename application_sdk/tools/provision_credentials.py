"""Provision credentials for local development.

Usage:
    uv run poe provision-credentials --body '{"host": "localhost", ...}'
    uv run poe provision-credentials --from-file creds.json
"""

import argparse
import json

import requests


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision credentials for local dev")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--body", help="JSON credential body as string")
    group.add_argument("--from-file", help="Path to JSON credential file")
    parser.add_argument("--port", default="8000", help="Handler port (default: 8000)")
    args = parser.parse_args()

    if args.body:
        body = json.loads(args.body)
    else:
        with open(args.from_file) as f:
            body = json.load(f)

    base_url = f"http://localhost:{args.port}"
    resp = requests.post(
        f"{base_url}/workflows/v1/dev/local-vault", json=body, timeout=10
    )
    resp.raise_for_status()
    result = resp.json()

    credential_guid = result.get("data", {}).get("credential_guid") or result.get(
        "credential_guid"
    )
    print(json.dumps({"credential_guid": credential_guid}, indent=2))  # noqa: T201
    print(  # noqa: T201
        f'\nUse this in your /start call: "credential_guid": "{credential_guid}"'
    )


if __name__ == "__main__":
    main()
