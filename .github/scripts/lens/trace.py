"""Step-by-step trace of a lens run, for the job log.

Every decision lens makes is printed as it happens, one collapsible section
per phase in the Actions log (``::group::``), so a run can be read top to
bottom: why it was admitted, which files and bundles, what the approach check
concluded, every turn and tool call each bundle's agent made, where each
comment landed, what the fact-check removed, what was posted.

Only metadata is printed: paths, counts, ids, statuses, short tool-argument
summaries. Never a prompt, a model's reasoning, source code beyond a one-line
quote, or a credential.
"""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator

_lock = threading.Lock()
_enabled = True


def _in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def set_enabled(on: bool) -> None:
    global _enabled
    _enabled = on


def line(msg: str) -> None:
    if not _enabled:
        return
    with _lock:
        print(f"lens: {msg}", file=sys.stderr, flush=True)


@contextmanager
def group(title: str) -> Iterator[None]:
    """A collapsible section in the Actions log (a plain header elsewhere)."""
    if not _enabled:
        yield
        return
    with _lock:
        print(
            f"::group::lens · {title}" if _in_actions() else f"── lens · {title}",
            file=sys.stderr,
            flush=True,
        )
    try:
        yield
    finally:
        if _in_actions():
            with _lock:
                print("::endgroup::", file=sys.stderr, flush=True)


def short(value: Any, limit: int = 120) -> str:
    """A one-line, length-capped rendering of a tool argument or value."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def args_summary(name: str, args: dict[str, Any]) -> str:
    """What a tool call asked for, in a few words — never a full payload."""
    if name == "code_comment":
        items = args.get("comments") or []
        sev = {}
        for c in items:
            if isinstance(c, dict):
                sev[c.get("severity", "?")] = sev.get(c.get("severity", "?"), 0) + 1
        return f"{len(items)} comment(s) " + ", ".join(
            f"{k}×{v}" for k, v in sev.items()
        )
    if name == "read_file":
        return f"{args.get('file_path')} lines {args.get('start_line') or 1}-{args.get('end_line') or '…'}"
    if name == "search_code":
        glob = f" in {args.get('path_glob')}" if args.get("path_glob") else ""
        return f"{short(args.get('search_text'), 60)!r}{glob}"
    if name == "find_symbol":
        return str(args.get("name"))
    if name == "read_diff":
        return ", ".join(args.get("path_array") or [])[:120]
    if name == "task_done":
        return str(args.get("state"))
    if name == "approach_verdict":
        return (
            f"verdict={args.get('verdict')} concerns={len(args.get('concerns') or [])}"
        )
    return short(args, 80)
