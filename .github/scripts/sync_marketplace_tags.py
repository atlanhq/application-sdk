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

import http.client
import os
import sys
import urllib.error
import urllib.request

APPS_PATH = "/api/service/marketplace/apps"
TIMEOUT_S = 60


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the bearer token never leaves ``BASE_URL``.

    urllib follows 30x by default and re-sends ``Authorization`` to the new
    location, which may be another origin or plain HTTP. Returning ``None``
    surfaces the 30x as an ``HTTPError``, which ``sync`` reports as a warning,
    matching the publish ``curl`` call, which does not follow redirects.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def post(url: str, body: str, token: str) -> tuple[int, str]:
    """POST *body* and return ``(status, response text)``; ``0`` on transport errors.

    Never raises: a malformed response (``http.client.HTTPException``, e.g.
    ``BadStatusLine``) or an unusable URL (``ValueError``) is a transport error
    too, so the post-publish step stays warn-only.
    """
    try:
        req = urllib.request.Request(
            url,
            data=body.encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 — https tenant URL from the caller's base_url input
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode(errors="replace")
        except (http.client.HTTPException, OSError) as read_exc:
            text = str(read_exc)
        return exc.code, text
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
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
