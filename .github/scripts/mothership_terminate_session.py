#!/usr/bin/env python3
"""Stop a mothership sandbox session after its GitHub Actions job is cancelled.

Cancelling the `sdk-review` job does not stop the sandbox. mothership runs it
on a thread-pool executor with a detached asyncio finalize task
(``harness/api/routers/sandbox_api.py::_sse_execution_wrapper``), and its SSE
queue drops on overflow instead of applying backpressure. Killing ``curl``
only closes the client end of the stream — the run continues, keeps billing
against ``max_timeout_seconds``, and still posts its review and commit status
to the PR using the sandbox's own ``gh`` token, minutes after the workflow
reports "cancelled".

``DELETE /api/sandbox/session/{session_id}?destroy=true`` is the documented
way to actually stop it (see mothership ``docs/reference/rover-direct-api.md``).

This runs inside the cancellation grace period as best-effort cleanup, so it
never exits non-zero: masking the real job outcome with a teardown error helps
nobody. Every branch is reported as an annotation instead.

Branching logic lives here (a tested script) rather than inlined in the
workflow YAML, per docs/standards/ci.md.

A run can boot more than one sandbox: `sdk_review_dispatch.py` re-dispatches
once on a different model when the first sandbox dies on a hard error, and each
attempt gets its OWN session id (mothership reads a reused id as a follow-up and
tries to resume the dead conversation). The workflow cannot know which attempt
was live when the cancel landed, so this stops every id the dispatcher could
have used — the ids are derived from the same helper, and an id that never
existed comes back 404, which is already handled as "nothing to stop".

Environment:
    MOTHERSHIP_URL   base URL, e.g. https://mothership.atlan.dev
    HARNESS_TOKEN    bearer token for the sandbox API
    SESSION_ID       base session id, from the workflow's `session` step
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))

from sdk_review_dispatch import (  # noqa: E402  (needs the sys.path bootstrap)
    MAX_DISPATCH_ATTEMPTS,
    attempt_session_id,
)

TIMEOUT_SECONDS = 30
BODY_PREVIEW_CHARS = 500

# A urllib-shaped callable: (url, token) -> (status_code, body_text).
Requester = Callable[[str, str], "tuple[int, str]"]


def _default_requester(url: str, token: str) -> tuple[int, str]:
    """DELETE `url` with a bearer token, returning (status, body).

    Maps every failure mode onto a status code so callers never branch on
    exception type: an HTTP error keeps its real code, and a transport
    failure (DNS, VPN down, timeout) reports 0.
    """
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # transport-level: DNS, VPN down, timeout
        return 0, str(e)


def terminate(
    base_url: str,
    token: str,
    session_id: str,
    requester: Requester | None = None,
) -> str:
    """Ask mothership to stop `session_id`; return a human-readable outcome.

    Never raises: this is teardown. The returned string is what the caller
    prints, and any annotation is emitted here.

    `requester` is resolved at call time rather than bound as a default
    argument so tests can substitute the module attribute and never reach
    the network.
    """
    send = requester or _default_requester
    url = f"{base_url.rstrip('/')}/api/sandbox/session/{session_id}?destroy=true"
    status, body = send(url, token)

    preview = body[:BODY_PREVIEW_CHARS].strip()
    if preview:
        print(preview)

    if 200 <= status < 300:
        return f"Sandbox termination requested for {session_id} (HTTP {status})."
    if status == 404:
        print(
            f"::notice::Session {session_id} is unknown to mothership — "
            "it already finished or never started."
        )
        return f"Nothing to stop for {session_id} (HTTP 404)."
    print(
        f"::warning::Could not stop sandbox {session_id} (HTTP {status}). "
        "It may keep running and still post a review to this PR."
    )
    return f"Termination request failed for {session_id} (HTTP {status})."


def main(argv: list[str] | None = None) -> int:
    base_url = os.environ.get("MOTHERSHIP_URL", "").strip()
    token = os.environ.get("HARNESS_TOKEN", "").strip()
    session_id = os.environ.get("SESSION_ID", "").strip()

    # Missing config is not a failure worth surfacing during teardown — the
    # workflow gates this step on session_id already, and a token/URL gap is
    # loud everywhere else in the job.
    if not session_id:
        print("::notice::No SESSION_ID — nothing to terminate.")
        return 0
    if not base_url or not token:
        print(
            "::warning::MOTHERSHIP_URL or HARNESS_TOKEN unset — cannot stop the sandbox."
        )
        return 0

    for attempt in range(1, MAX_DISPATCH_ATTEMPTS + 1):
        sid = attempt_session_id(session_id, attempt)
        print(f"Job cancelled — asking mothership to stop session {sid}.")
        print(terminate(base_url, token, sid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
