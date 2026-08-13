"""The fleet-wide watchdog settings, and how a task's declaration resolves against them.

FND-296 ships the stall watchdog on by default in ``warn`` and raises
``start_to_close`` to a 24h backstop in the same release (ADR-0018 → *Rollout*
step 3). Both halves of that sentence are load-bearing and both are pinned here:

- ``warn`` really is what an app that declares nothing gets, because an opt-in
  would have bought nothing (warn cannot fail an activity) at the cost of a
  ~20-team coordination step.
- ``off`` really is a kill-switch — it beats a per-task ``enforce``, because the
  only time it gets thrown is an incident, and a switch a decorator can out-vote
  is no switch.
- The allowance has *no* such override, deliberately: an env var that could
  silently shrink an allowance a task declared for itself would be a fleet-wide
  false-kill generator.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from application_sdk._runtime.progress import DEFAULT_MAX_NO_PROGRESS_SECONDS
from application_sdk.execution import progress as progress_mod
from application_sdk.execution.progress import (
    ProgressWatchdogMode,
    _load_max_no_progress_seconds,
    _load_watchdog_mode,
    resolve_max_no_progress_seconds,
    resolve_watchdog_mode,
)


class TestModeEnvVar:
    def test_unset_means_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The headline property of this release: nobody opts in."""
        monkeypatch.delenv("ATLAN_PROGRESS_WATCHDOG", raising=False)

        assert _load_watchdog_mode() is ProgressWatchdogMode.WARN

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("off", ProgressWatchdogMode.OFF),
            ("warn", ProgressWatchdogMode.WARN),
            ("enforce", ProgressWatchdogMode.ENFORCE),
            ("  ENFORCE  ", ProgressWatchdogMode.ENFORCE),
        ],
    )
    def test_every_mode_is_settable(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: ProgressWatchdogMode
    ) -> None:
        monkeypatch.setenv("ATLAN_PROGRESS_WATCHDOG", raw)

        assert _load_watchdog_mode() is expected

    def test_a_typo_falls_back_to_warn_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad value in one deployment manifest costs one config value, not a worker.

        Raising here would stop the process booting, which is a far worse outcome
        than reporting gaps nobody asked for.
        """
        monkeypatch.setenv("ATLAN_PROGRESS_WATCHDOG", "enfroce")

        assert _load_watchdog_mode() is ProgressWatchdogMode.WARN

    def test_the_import_time_constant_is_what_production_reads(self) -> None:
        """The constant binds once at import, so that binding is the real surface.

        Subprocess, for the reason the sibling run-length test gives: re-importing
        the module in-process would mint a second ``ProgressWatchdogMode`` and
        break ``is`` comparisons for every test that imported it by name.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; "
                "os.environ['ATLAN_PROGRESS_WATCHDOG'] = 'off'; "
                "from application_sdk.execution import progress as m; "
                "assert m.PROGRESS_WATCHDOG_MODE is m.ProgressWatchdogMode.OFF, "
                "f'constant bound {m.PROGRESS_WATCHDOG_MODE}, not the env var'",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


class TestAllowanceEnvVar:
    def test_unset_means_the_adr_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLAN_MAX_NO_PROGRESS_SECONDS", raising=False)

        assert _load_max_no_progress_seconds() == DEFAULT_MAX_NO_PROGRESS_SECONDS == 900

    def test_a_positive_value_is_taken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_MAX_NO_PROGRESS_SECONDS", "1800")

        assert _load_max_no_progress_seconds() == 1800.0

    @pytest.mark.parametrize("raw", ["0", "-1", "fifteen minutes"])
    def test_an_unusable_value_falls_back_rather_than_disabling(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """Zero would stall every attempt on its first tick.

        In an enforcing app that turns one typo into a fleet-wide kill switch, so
        the allowance never resolves to something the watchdog would act on
        immediately. Turning the watchdog off is what ``off`` is for.
        """
        monkeypatch.setenv("ATLAN_MAX_NO_PROGRESS_SECONDS", raw)

        assert _load_max_no_progress_seconds() == DEFAULT_MAX_NO_PROGRESS_SECONDS


class TestResolveMode:
    def test_no_declaration_inherits_the_fleet_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            progress_mod, "PROGRESS_WATCHDOG_MODE", ProgressWatchdogMode.WARN
        )

        assert resolve_watchdog_mode(None) is ProgressWatchdogMode.WARN

    def test_a_declaration_beats_the_fleet_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rollout step 6: an app flips itself to enforce when it wants the guarantee."""
        monkeypatch.setattr(
            progress_mod, "PROGRESS_WATCHDOG_MODE", ProgressWatchdogMode.WARN
        )

        assert resolve_watchdog_mode(ProgressWatchdogMode.ENFORCE) is (
            ProgressWatchdogMode.ENFORCE
        )

    def test_a_declaration_beats_a_stronger_fleet_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The boundary that proves a declaration — not just ``None`` — wins.

        Fleet ``enforce`` with a task pinned to ``warn`` resolves to ``warn``:
        the matrix above pins a declaration beating a weaker fleet mode, and this
        pins it beating a stronger one. Only ``off`` overrides a declaration.
        """
        monkeypatch.setattr(
            progress_mod, "PROGRESS_WATCHDOG_MODE", ProgressWatchdogMode.ENFORCE
        )

        assert resolve_watchdog_mode(ProgressWatchdogMode.WARN) is (
            ProgressWatchdogMode.WARN
        )

    def test_the_fleet_can_be_moved_without_touching_a_decorator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            progress_mod, "PROGRESS_WATCHDOG_MODE", ProgressWatchdogMode.ENFORCE
        )

        assert resolve_watchdog_mode(None) is ProgressWatchdogMode.ENFORCE

    @pytest.mark.parametrize(
        "declared",
        [None, ProgressWatchdogMode.WARN, ProgressWatchdogMode.ENFORCE],
    )
    def test_off_in_the_environment_beats_every_declaration(
        self, monkeypatch: pytest.MonkeyPatch, declared: ProgressWatchdogMode | None
    ) -> None:
        """The kill-switch has to actually switch things off.

        An operator reaches for it mid-incident; a task pinned to ``enforce`` in
        source they may not own must not out-vote it.
        """
        monkeypatch.setattr(
            progress_mod, "PROGRESS_WATCHDOG_MODE", ProgressWatchdogMode.OFF
        )

        assert resolve_watchdog_mode(declared) is ProgressWatchdogMode.OFF


class TestResolveAllowance:
    def test_no_declaration_inherits_the_fleet_allowance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(progress_mod, "MAX_NO_PROGRESS_SECONDS", 1200.0)

        assert resolve_max_no_progress_seconds(None) == 1200.0

    @pytest.mark.parametrize("declared", [60.0, 7200.0])
    def test_a_declared_allowance_always_wins(
        self, monkeypatch: pytest.MonkeyPatch, declared: float
    ) -> None:
        """In both directions — there is no kill-switch analogue for the allowance.

        An env var that could shrink a declared allowance would false-kill the
        very sites whose authors had already sized them.
        """
        monkeypatch.setattr(progress_mod, "MAX_NO_PROGRESS_SECONDS", 900.0)

        assert resolve_max_no_progress_seconds(declared) == declared
