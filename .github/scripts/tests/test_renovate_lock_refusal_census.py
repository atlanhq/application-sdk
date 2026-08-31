"""Tests for the lock-refusal census (FND-909).

The census is the thing that decides whether the lock lane is healthy, so its
verdicts need the same regression cover as the reaper's. Its first live run
already proved why: an over-broad tripwire test reported six ordinary refreshes
as refusals, which would have made the whole signal useless.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))

import renovate_lock_refusal_census as census  # noqa: E402
import renovate_uv_lock_bounded as bounded  # noqa: E402

PACKAGES = '\n[[package]]\nname = "boto3"\nversion = "1.43.78"\n'


def lock_with(options: str) -> str:
    if not options:
        return f"version = 1\nrevision = 3\n{PACKAGES}"
    return f"version = 1\nrevision = 3\n\n{options}\n{PACKAGES}"


ORDINARY = lock_with("")
UNSTAMPED = lock_with('[options]\nexclude-newer-span = "P3D"')
WINDOW_EMPTY = lock_with(
    '[options]\nexclude-newer-span = "P3D"  # refusal: window-empty'
)
ROLLBACK = lock_with('[options]\nexclude-newer-span = "P3D"  # refusal: rollback')
# atlan-bw-app's shape: a repo bounded through its own pyproject.toml. uv writes
# both keys, and this must never read as a refusal.
NATIVELY_BOUND = lock_with(
    '[options]\nexclude-newer = "0001-01-01T00:00:00Z"\nexclude-newer-span = "P7D"'
)


def fake_api(*, files, lock_text, committed="2026-08-28T00:00:00Z"):
    """A `_get` stand-in serving one PR's file list, head commit and lock."""

    def get(_token, url):
        if "/pulls/" in url and url.endswith("/files?per_page=100"):
            return [{"filename": f} for f in files]
        if "/pulls/" in url:
            return {"head": {"sha": "deadbee"}}
        if "/commits/" in url:
            return {"commit": {"committer": {"date": committed}}}
        if "/contents/" in url:
            return {"content": base64.b64encode(lock_text.encode()).decode()}
        raise AssertionError(f"unexpected url {url}")

    return get


class TestInspect:
    def verdict(self, monkeypatch, *, files, lock_text):
        monkeypatch.setattr(census, "_get", fake_api(files=files, lock_text=lock_text))
        return census.inspect("tok", "atlanhq/x", 1)

    def test_ordinary_single_file_refresh(self, monkeypatch):
        verdict, reason, _ = self.verdict(
            monkeypatch, files=["uv.lock"], lock_text=ORDINARY
        )
        assert (verdict, reason) == ("ordinary", None)

    def test_multi_file_pr_is_ordinary(self, monkeypatch):
        verdict, reason, _ = self.verdict(
            monkeypatch, files=["uv.lock", "pyproject.toml"], lock_text=WINDOW_EMPTY
        )
        assert (verdict, reason) == ("ordinary", None)

    def test_unstamped_tripwire(self, monkeypatch):
        verdict, reason, _ = self.verdict(
            monkeypatch, files=["uv.lock"], lock_text=UNSTAMPED
        )
        assert (verdict, reason) == ("unstamped_tripwire", None)

    def test_a_natively_bounded_repo_is_ordinary_not_a_refusal(self, monkeypatch):
        # The false positive the first live run produced: six repos whose locks
        # carry uv's own [options] table were reported as frozen refusals.
        verdict, reason, _ = self.verdict(
            monkeypatch, files=["uv.lock"], lock_text=NATIVELY_BOUND
        )
        assert (verdict, reason) == ("ordinary", None)

    def test_stamped_self_healing_refusal(self, monkeypatch):
        verdict, reason, _ = self.verdict(
            monkeypatch, files=["uv.lock"], lock_text=WINDOW_EMPTY
        )
        assert (verdict, reason) == ("refusal", bounded.REFUSAL_WINDOW_EMPTY)

    def test_stamped_standing_fault(self, monkeypatch):
        verdict, reason, _ = self.verdict(
            monkeypatch, files=["uv.lock"], lock_text=ROLLBACK
        )
        assert (verdict, reason) == ("refusal", bounded.REFUSAL_ROLLBACK)

    def test_reports_the_head_commit_date_not_the_pr_age(self, monkeypatch):
        # Renovate rewrites a lock branch in place, so a PR opened last week can
        # carry a refusal written an hour ago. The branch clock is the only one
        # that dates the refusal.
        _, _, committed = self.verdict(
            monkeypatch, files=["uv.lock"], lock_text=WINDOW_EMPTY
        )
        assert committed == "2026-08-28T00:00:00Z"


class TestMain:
    def run(self, monkeypatch, capsys, *, lock_text, committed, argv=None):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setattr(
            census,
            "open_lock_prs",
            lambda _t: [
                {
                    "number": 1,
                    "repository_url": "https://api.github.com/repos/atlanhq/x",
                }
            ],
        )
        monkeypatch.setattr(
            census,
            "_get",
            fake_api(files=["uv.lock"], lock_text=lock_text, committed=committed),
        )
        code = census.main(argv or [])
        return code, capsys.readouterr()

    def hours_ago(self, hours: float) -> str:
        moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_a_fresh_self_healing_refusal_passes(self, monkeypatch, capsys):
        # One pass old: the reaper has not had its turn yet, and that is fine.
        code, out = self.run(
            monkeypatch, capsys, lock_text=WINDOW_EMPTY, committed=self.hours_ago(3)
        )
        assert code == 0
        assert "FROZEN self-healing: 0" in out.out

    def test_a_self_healing_refusal_surviving_two_passes_fails(
        self, monkeypatch, capsys
    ):
        # The whole point of the script: this is the shape that means the reaper
        # is not firing, and it has to be a non-zero exit, not a line of prose.
        code, out = self.run(
            monkeypatch, capsys, lock_text=WINDOW_EMPTY, committed=self.hours_ago(30)
        )
        assert code == 1
        assert "FROZEN self-healing: 1" in out.out
        assert "the reaper is not firing" in out.err

    def test_an_old_standing_fault_does_not_fail_the_run(self, monkeypatch, capsys):
        # A yanked pin is meant to stay red until a human clears it. Failing on
        # it would train people to ignore the census.
        code, out = self.run(
            monkeypatch, capsys, lock_text=ROLLBACK, committed=self.hours_ago(200)
        )
        assert code == 0
        assert "standing faults:     1" in out.out

    def test_an_old_unstamped_tripwire_does_not_fail_the_run(self, monkeypatch, capsys):
        # Pre-FND-909 refusals are reported for the manual migration, but the
        # reaper is not expected to have taken them.
        code, out = self.run(
            monkeypatch, capsys, lock_text=UNSTAMPED, committed=self.hours_ago(200)
        )
        assert code == 0
        assert "unstamped tripwire:  1" in out.out

    def test_an_old_ordinary_refresh_is_not_a_finding(self, monkeypatch, capsys):
        code, out = self.run(
            monkeypatch, capsys, lock_text=ORDINARY, committed=self.hours_ago(500)
        )
        assert code == 0
        assert "FROZEN self-healing: 0" in out.out
        assert "standing faults:     0" in out.out

    def test_json_output_is_machine_readable(self, monkeypatch, capsys):
        code, out = self.run(
            monkeypatch,
            capsys,
            lock_text=WINDOW_EMPTY,
            committed=self.hours_ago(30),
            argv=["--json"],
        )
        payload = json.loads(out.out)
        assert code == 1
        assert payload["total_open"] == 1
        assert payload["frozen_self_healing"][0]["repo"] == "atlanhq/x"
        assert (
            payload["frozen_self_healing"][0]["reason"] == bounded.REFUSAL_WINDOW_EMPTY
        )

    def test_missing_token_fails_loudly(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert census.main([]) == 1

    def test_an_uninspectable_pr_warns_and_is_skipped(self, monkeypatch, capsys):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setattr(
            census,
            "open_lock_prs",
            lambda _t: [
                {
                    "number": 1,
                    "repository_url": "https://api.github.com/repos/atlanhq/x",
                }
            ],
        )

        def boom(_token, _url):
            raise TimeoutError("api down")

        monkeypatch.setattr(census, "_get", boom)
        assert census.main([]) == 0
        assert "::warning::" in capsys.readouterr().out


@pytest.mark.parametrize("hours,expected", [(8.9, False), (9.1, True)])
def test_the_two_pass_boundary_is_where_it_says_it_is(hours, expected):
    # Two 4-hourly passes plus a pass's own runtime. Asserted directly so the
    # threshold cannot drift away from the cron it is derived from.
    assert (hours > census.SURVIVED_TWO_PASSES.total_seconds() / 3600) is expected
