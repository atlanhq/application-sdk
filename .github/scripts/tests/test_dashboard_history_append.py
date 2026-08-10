"""Guards the history-append invariant in .github/workflows/update-dashboard.yaml.

All three dashboard jobs (security/Trivy, conformance, test-readiness) append a
per-repo trend line to S3 with the same download -> merge -> upload sequence::

    aws s3 cp s3://.../history/<slug>.jsonl /tmp/existing.jsonl 2>/dev/null || true
    cat /tmp/existing.jsonl /tmp/.../history_<slug>.jsonl | sort -u > /tmp/merged.jsonl
    aws s3 cp /tmp/merged.jsonl s3://.../history/<slug>.jsonl

On a repo's FIRST publish the S3 object does not exist, so the `cp` no-ops
(swallowed by `|| true`) and never creates the local file. `cat` then exits 1,
and because the step runs under `set -euo pipefail` the whole step dies —
*after* the summary upload has already succeeded. The repo shows up on the
dashboard but never gets a single history line, and since the history object is
never created the next run fails identically. It never self-heals.

Only the test-readiness job had the `touch` guard; the Trivy and conformance
jobs did not, which is why the Conformance tab had no trend data for any repo.
This test asserts every `cat`-merge in the file is preceded by a `touch` of the
file it reads.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).parent.parent.parent / "workflows" / "update-dashboard.yaml"

# `cat <existing> <generated> ... | sort -u` — capture the first operand, which
# is the S3-sourced file that may legitimately not exist.
_CAT_MERGE = re.compile(r"^\s*cat\s+(/tmp/[\w./${}-]+)", re.MULTILINE)


def _text() -> str:
    return WORKFLOW.read_text()


def test_workflow_is_present():
    assert WORKFLOW.is_file(), f"expected {WORKFLOW} to exist"


def test_every_history_merge_touches_its_source_first():
    text = _text()
    merges = _CAT_MERGE.findall(text)

    # Three dashboards, three merges. If this count changes, a fourth dashboard
    # was added (or one removed) and its history path needs the same guard.
    assert len(merges) == 3, f"expected 3 history merges, found {len(merges)}: {merges}"

    for source in merges:
        cat_at = text.index(f"cat {source}")
        preamble = text[:cat_at]
        assert f"touch {source}" in preamble, (
            f"`cat {source}` is not preceded by `touch {source}` — a first "
            f"publish will exit 1 under `set -euo pipefail` after the summary "
            f"has already been uploaded, leaving the repo permanently without "
            f"trend history"
        )


def test_each_dashboard_history_file_is_distinct():
    """Guards against copy-paste that would make two jobs share a temp file."""
    merges = _CAT_MERGE.findall(_text())
    assert len(set(merges)) == len(merges), f"duplicate history temp files: {merges}"


def test_history_downloads_still_tolerate_a_missing_object():
    """`|| true` must stay — the touch guard replaces the crash, not the 404."""
    text = _text()
    for source in _CAT_MERGE.findall(text):
        cp_line = next(
            (ln for ln in text.splitlines() if source in ln and "aws s3 cp" in ln),
            None,
        )
        # Some cp invocations are line-wrapped; fall back to a window search.
        if cp_line is None:
            idx = text.index(f"touch {source}")
            window = text[max(0, idx - 400) : idx]
            assert "|| true" in window, f"missing `|| true` guard near {source}"
        else:
            assert "|| true" in cp_line or "2>/dev/null" in cp_line


def test_conformance_job_no_longer_hardcodes_sarif_series():
    """The 7-slug loop must not come back — series come off the run itself."""
    text = _text()
    assert "for slug in ci error-handling" not in text
    assert "fetch_conformance_sarif.py" in text
