"""Tests for .github/scripts/vuln_auto_merge_gate.py — the auto-merge boundary."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import vuln_auto_merge_gate as gate

TRUSTED = {"atlan-ci", "mothership-ai[bot]"}
LABEL = "vuln-auto-merge"


# ---------------------------------------------------------------------------
# classify_shape — the path allowlist
# ---------------------------------------------------------------------------


def test_allowlist_only_is_allowlist_shape():
    assert classify(".security/base-allowlist.json") == "allowlist"


def classify(*files):
    return gate.classify_shape(list(files))


def test_bump_subset_is_bump_shape():
    assert classify("uv.lock") == "bump"
    assert classify("pyproject.toml", "uv.lock") == "bump"
    assert classify("pyproject.toml", "uv.lock", "requirements.txt") == "bump"


def test_empty_is_none():
    assert classify() is None
    assert gate.classify_shape([""]) is None


def test_allowlist_plus_extra_file_is_none():
    # An allowlist PR that also touches anything else must NOT match.
    assert classify(".security/base-allowlist.json", "README.md") is None


def test_bump_plus_source_file_is_none():
    # Plan verification (c): a bump that also edits a .py source file is refused.
    assert classify("uv.lock", "application_sdk/foo.py") is None
    assert classify("pyproject.toml", "Dockerfile") is None


def test_mixed_allowlist_and_bump_is_none():
    # The two shapes are strictly separate.
    assert classify(".security/base-allowlist.json", "uv.lock") is None


def test_same_named_file_in_subdir_is_none():
    # Only root-level manifests count; a nested pyproject.toml must not ride.
    assert classify("packages/conformance/pyproject.toml") is None
    assert classify("subdir/uv.lock") is None


# ---------------------------------------------------------------------------
# evaluate — full gate
# ---------------------------------------------------------------------------


def _eval(**overrides):
    base = dict(
        author="atlan-ci",
        state="open",
        draft=False,
        head_sha="abc",
        eval_sha="abc",
        labels=[LABEL],
        filenames=[".security/base-allowlist.json"],
        label_name=LABEL,
        trusted_authors=TRUSTED,
    )
    base.update(overrides)
    return gate.evaluate(**base)


def test_evaluate_happy_path_allowlist():
    ok, _reason, shape = _eval()
    assert ok and shape == "allowlist"


def test_evaluate_happy_path_bump():
    ok, _reason, shape = _eval(filenames=["pyproject.toml", "uv.lock"])
    assert ok and shape == "bump"


def test_evaluate_untrusted_author_rejected():
    ok, _r, _s = _eval(author="random-user")
    assert not ok


def test_evaluate_mothership_bot_trusted():
    ok, _r, _s = _eval(author="mothership-ai[bot]")
    assert ok


def test_evaluate_draft_rejected():
    assert not _eval(draft=True)[0]


def test_evaluate_closed_rejected():
    assert not _eval(state="closed")[0]


def test_evaluate_head_moved_rejected():
    # Plan verification: race guard.
    assert not _eval(head_sha="def", eval_sha="abc")[0]


def test_evaluate_missing_label_rejected():
    # Plan verification (d): no label → refused.
    assert not _eval(labels=[])[0]


def test_evaluate_non_matching_files_rejected():
    assert not _eval(filenames=["application_sdk/foo.py"])[0]


# ---------------------------------------------------------------------------
# process_pr — wired with a fake gh runner
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Minimal gh stub: returns canned JSON per api call, records actions."""

    def __init__(self, meta, files, approved_count=0):
        self.meta = meta
        self.files = files
        self.approved_count = approved_count
        self.reviews = []
        self.merges = []

    def __call__(self, cmd, check=False, capture_output=False, text=False):
        joined = " ".join(cmd)
        out = ""
        if "/files" in joined:
            out = "\n".join(self.files)
        elif "/reviews" in joined:
            out = str(self.approved_count)
        elif "/pulls/" in joined and "--jq" in joined:
            out = json.dumps(self.meta)
        elif cmd[:3] == ["gh", "pr", "review"]:
            self.reviews.append(cmd)
        elif cmd[:3] == ["gh", "pr", "merge"]:
            self.merges.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=out, stderr=""
        )


APPROVER = "atlan-ci"


def _meta(author="mothership-ai[bot]", **kw):
    base = {
        "author": author,
        "state": "open",
        "draft": False,
        "head_sha": "abc",
        "labels": [LABEL],
    }
    base.update(kw)
    return base


def test_process_pr_approves_and_merges_rover_pr():
    # Rover PR (mothership-ai[bot]) → atlan-ci approves + auto-merges.
    r = _FakeRunner(_meta(), [".security/base-allowlist.json"])
    acted = gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert acted is True
    assert len(r.reviews) == 1
    assert len(r.merges) == 1
    assert "--auto" in r.merges[0]


def test_process_pr_self_authored_skips_approval():
    # Reconcile removal PR authored by atlan-ci (the approver) → NO self-approval
    # (would 422); bypass-merge only.
    r = _FakeRunner(_meta(author="atlan-ci"), [".security/base-allowlist.json"])
    acted = gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert acted is False
    assert r.reviews == []  # never self-approve
    assert len(r.merges) == 1  # but still auto-merge (bypass actor)


def test_process_pr_skips_when_files_unsafe():
    r = _FakeRunner(_meta(), ["uv.lock", "application_sdk/foo.py"])
    acted = gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert acted is False
    assert r.reviews == []
    assert r.merges == []


def test_process_pr_idempotent_when_already_approved():
    r = _FakeRunner(_meta(), [".security/base-allowlist.json"], approved_count=1)
    acted = gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert acted is False
    # No new approval, but auto-merge is (re-)ensured.
    assert r.reviews == []
    assert len(r.merges) == 1
