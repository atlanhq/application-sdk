#!/usr/bin/env python3
"""Sync an app's ``atlan.yaml`` tags to the marketplace after publish.

``POST /marketplace/publish`` creates a version and has no ``tags`` field, so
app-level tags only reach the marketplace through the app update
``POST /marketplace/apps`` (the call ``atlan app register`` makes). The body
comes from ``parse_atlan_yaml.py``'s ``app_sync_body`` output.

Warn-only: the version is already published when this runs, so a failed sync
emits a ``::warning::`` and exits 0 rather than failing the release.

Environment:
    BASE_URL           tenant base URL, e.g. https://tenant.atlan.com
    APP_PUBLISH_TOKEN  bearer token, the same one the publish step uses
    APP_SYNC_BODY      JSON body; empty means nothing to sync
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

APPS_PATH = "/api/service/marketplace/apps"
TIMEOUT_S = 60


def post(url: str, body: str, token: str) -> tuple[int, str]:
    """POST *body* and return ``(status, response text)``; ``0`` on transport errors."""
    req = urllib.request.Request(
        url,
        data=body.encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 — https tenant URL from the caller's base_url input
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc)


def sync(base_url: str, token: str, body: str) -> str:
    """Send the sync and return the line to log. Never raises on HTTP failure."""
    if not body:
        return "No tags in atlan.yaml; nothing to sync."
    status, text = post(base_url.rstrip("/") + APPS_PATH, body, token)
    if 200 <= status < 300:
        return f"Synced marketplace tags: {text}"
    return (
        "::warning title=Tag sync failed::Version published, but marketplace "
        f"tags were not updated (HTTP {status or 'error'}): {text}"
    )


def main() -> None:
    line = sync(
        os.environ.get("BASE_URL", ""),
        os.environ.get("APP_PUBLISH_TOKEN", ""),
        os.environ.get("APP_SYNC_BODY", "").strip(),
    )
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and line.startswith("Synced"):
        with open(summary, "a") as fh:
            fh.write("Synced marketplace tags from `atlan.yaml`.\n")


if __name__ == "__main__":
    main()
    sys.exit(0)
