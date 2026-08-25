"""Artifact-validation posture: soft/hard, the second axis, and the block (FND-692).

Three properties are under test, and they are the three that make a blocking check
safe to ship:

1. **Blocking is always something someone chose.** Only the literal ``"hard"``
   enforces, at either precedence level; every other value — a typo, a ``"true"``,
   an unset variable — resolves to soft. A run is never blocked by accident.
2. **The soft rows forecast hard mode exactly.** ``blocked`` and ``would_block``
   come off one call on one report, so what an app sees in soft mode is what it
   will get when it graduates (FND-694). A second, drifting derivation would make
   the measured false-positive rate describe something other than the behaviour
   being graduated to.
3. **A defect in the check never fails a healthy run.** Everything classified
   ``validator_broken`` proceeds in *both* postures — the artifact-side twin of the
   preflight gate's ``gate_broken`` fail-open.

Outcome rows are captured by patching the module's own logger, as in
:mod:`tests.unit.validation.test_interceptor`;
:mod:`tests.unit.validation.test_artifact_event_fields` is what proves the new
attribute keys survive ``_build_extra_dict`` on the way to OTLP.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import orjson
import pytest

from application_sdk.constants import ARTIFACT_VALIDATION_MODE_ENV
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.observability.events import (
    ARTIFACT_VALIDATION_EVENT,
    ARTIFACT_VALIDATION_POSTURE_EVENT,
    OUTCOME_EVENT_NAMES,
)
from application_sdk.observability.logger_adaptor import (
    _KNOWN_EXTRA_KEYS,
    ARTIFACT_CLASSIFICATION_KEY,
    ARTIFACT_ENFORCEMENT_KEY,
    ARTIFACT_MODE_KEY,
)
from application_sdk.validation import interceptor as interceptor_module
from application_sdk.validation.artifacts import (
    CLASSIFICATION_ARTIFACT_UNVERIFIABLE,
    CLASSIFICATION_VALIDATOR_BROKEN,
    CLASSIFICATION_VERDICT,
    ENFORCEMENT_BLOCKED,
    ENFORCEMENT_NONE,
    ENFORCEMENT_WOULD_BLOCK,
    MODE_HARD,
    MODE_OFF,
    MODE_SOFT,
    ArtifactValidationFailure,
    ArtifactValidationReport,
    artifact_enforcement,
    artifact_validation_mode,
)
from application_sdk.validation.interceptor import (
    ARTIFACT_SIDE_HANDOFF,
    ARTIFACT_SIDE_INGEST,
    ArtifactValidationBlockedError,
    artifact_validation_enforced,
    log_artifact_validation_posture,
    resolve_artifact_enforcement,
    validate_artifacts,
)

# ---------------------------------------------------------------------------
# Contracts / fixtures
# ---------------------------------------------------------------------------


class _PostureIn(Input, allow_unbounded_fields=True):
    """Stands in for an entry point's public input contract."""

    source: FileReference | None = None


class _PostureOut(Output, allow_unbounded_fields=True):
    """Stands in for an entry point's public output contract."""

    queries: FileReference | None = None


class _PostureInternalOut(Output, allow_unbounded_fields=True):
    """An internal ``@task`` contract — never an entry point's boundary."""

    scratch: FileReference | None = None


class _TwoRefsOut(Output, allow_unbounded_fields=True):
    first: FileReference | None = None
    second: FileReference | None = None


_DECLARED = {
    "queries": {
        "format": "ndjson",
        "fields": [
            {"name": "QUERY_ID", "type": "string", "description": "the query id"},
            {"name": "START_TIME", "type": "timestamp", "description": "when it ran"},
        ],
    },
    "first": {
        "format": "ndjson",
        "fields": [
            {"name": "QUERY_ID", "type": "string", "description": "the query id"}
        ],
    },
    "second": {
        "format": "ndjson",
        "fields": [
            {"name": "QUERY_ID", "type": "string", "description": "the query id"}
        ],
    },
}


@pytest.fixture
def generated_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the contract source at a per-test generated tree."""
    generated = tmp_path / "generated"
    generated.mkdir()
    monkeypatch.setattr(
        "application_sdk.validation.sources.CONTRACT_GENERATED_DIR", str(generated)
    )
    (generated / "artifact_schemas.json").write_bytes(
        orjson.dumps({"version": 1, "schemas": _DECLARED})
    )
    return generated


def _ndjson(tmp_path: Path, name: str, records: list[dict[str, Any]]) -> str:
    path = tmp_path / name
    path.write_bytes(b"\n".join(orjson.dumps(r) for r in records) + b"\n")
    return str(path)


def _clean(tmp_path: Path, name: str = "clean.json") -> str:
    return _ndjson(
        tmp_path, name, [{"QUERY_ID": "q1", "START_TIME": "2026-08-25T10:00:00Z"}]
    )


def _stringified_timestamp(tmp_path: Path, name: str = "bad.json") -> str:
    """The 73-day RCA in one line: a timestamp column that became a string."""
    return _ndjson(tmp_path, name, [{"QUERY_ID": "q1", "START_TIME": "not-a-time"}])


def _rows(logger: Any) -> list[dict[str, Any]]:
    return [
        call.kwargs
        for call in logger.info.call_args_list
        if call.args and call.args[0] == ARTIFACT_VALIDATION_EVENT
    ]


async def _run(data: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Run the hook with a captured logger and return the emitted rows."""
    kwargs.setdefault("side", ARTIFACT_SIDE_HANDOFF)
    kwargs.setdefault("boundary_contracts", frozenset({_PostureOut, _TwoRefsOut}))
    with patch.object(interceptor_module, "logger") as logger:
        await validate_artifacts(data, **kwargs)
        return _rows(logger)


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


class _SoftApp:
    artifact_validation_mode = "soft"


class _HardApp:
    artifact_validation_mode = "hard"


class TestModeResolution:
    """Precedence: env > declared ClassVar > soft. Only ``"hard"`` enforces."""

    def test_undeclared_and_unset_is_soft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ARTIFACT_VALIDATION_MODE_ENV, raising=False)
        assert resolve_artifact_enforcement(_SoftApp) is False

    def test_declared_hard_with_no_env_is_hard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ARTIFACT_VALIDATION_MODE_ENV, raising=False)
        assert resolve_artifact_enforcement(_HardApp) is True

    def test_env_hard_overrides_a_declared_soft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ARTIFACT_VALIDATION_MODE_ENV, "hard")
        assert resolve_artifact_enforcement(_SoftApp) is True

    def test_env_soft_overrides_a_declared_hard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ops lever's whole point: stand a fleet down without an app release."""
        monkeypatch.setenv(ARTIFACT_VALIDATION_MODE_ENV, "soft")
        assert resolve_artifact_enforcement(_HardApp) is False

    @pytest.mark.parametrize("bad", ["true", "1", "block", "HARDCORE", "on"])
    def test_a_bad_env_value_falls_back_to_soft(
        self, bad: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo must never be the reason a run got blocked."""
        monkeypatch.setenv(ARTIFACT_VALIDATION_MODE_ENV, bad)
        assert resolve_artifact_enforcement(_HardApp) is False
        assert resolve_artifact_enforcement(_SoftApp) is False

    @pytest.mark.parametrize("bad", ["true", "yes", "HARDER"])
    def test_a_bad_declared_value_falls_back_to_soft(
        self, bad: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ARTIFACT_VALIDATION_MODE_ENV, raising=False)

        class _Typo:
            artifact_validation_mode = bad

        assert resolve_artifact_enforcement(_Typo) is False

    def test_an_empty_env_value_is_not_an_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So a deployment can leave the variable unset rather than restating the
        app's own default back at it — and an accidentally-blanked variable does
        not silently *disable* an app that declared hard."""
        monkeypatch.setenv(ARTIFACT_VALIDATION_MODE_ENV, "")
        assert resolve_artifact_enforcement(_HardApp) is True

    @pytest.mark.parametrize("spelling", ["HARD", " hard ", "Hard\n"])
    def test_case_and_whitespace_are_forgiven(
        self, spelling: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ARTIFACT_VALIDATION_MODE_ENV, spelling)
        assert resolve_artifact_enforcement(_SoftApp) is True

    def test_an_unresolvable_app_is_soft_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ARTIFACT_VALIDATION_MODE_ENV, raising=False)
        assert resolve_artifact_enforcement(None) is False

        class _NoSuchAttribute:
            pass

        assert resolve_artifact_enforcement(_NoSuchAttribute) is False

    def test_an_unregistered_app_name_is_soft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ARTIFACT_VALIDATION_MODE_ENV, raising=False)
        assert artifact_validation_enforced("no-such-app-fnd692") is False

    def test_a_registered_app_resolves_through_the_same_rule(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The activity seam holds an app *name*, the worker holds the class. Both
        must land on the same posture, or the boot-time row would describe a
        posture the activities do not run."""
        monkeypatch.delenv(ARTIFACT_VALIDATION_MODE_ENV, raising=False)
        from application_sdk.app.base import App

        class PostureProbeApp(App):
            artifact_validation_mode = "hard"

            async def run(self, input: _PostureIn) -> _PostureOut:  # noqa: D102
                return _PostureOut()

        assert artifact_validation_enforced("posture-probe-app") is True
        assert resolve_artifact_enforcement(PostureProbeApp) is True


class TestModeLabel:
    def test_the_three_resolved_postures(self) -> None:
        assert artifact_validation_mode(enforce=True) == MODE_HARD
        assert artifact_validation_mode(enforce=False) == MODE_SOFT
        assert artifact_validation_mode(enforce=True, enabled=False) == MODE_OFF

    def test_the_kill_switch_wins_over_a_hard_declaration(self) -> None:
        """A hard app on a deployment with the switch down blocks nothing, and the
        posture row must not promise enforcement that is not happening."""
        assert artifact_validation_mode(enforce=True, enabled=False) == MODE_OFF
        assert artifact_validation_mode(enforce=False, enabled=False) == MODE_OFF


# ---------------------------------------------------------------------------
# The second axis
# ---------------------------------------------------------------------------


def _flagged_report(*, boundary: bool = True) -> ArtifactValidationReport:
    return ArtifactValidationReport(
        artifact_format="ndjson",
        schema_source="contract",
        unit="record",
        fields_declared=2,
        total=10,
        passed=9,
        boundary=boundary,
        failures=[
            ArtifactValidationFailure(
                kind="type_mismatch",
                field="START_TIME",
                expected="timestamp",
                actual="string",
            )
        ],
    )


class TestClassificationAxis:
    """ "The artifact is unverifiable" and "our validator broke" are not the same."""

    def test_a_scan_that_ran_is_a_verdict(self) -> None:
        assert _flagged_report().classification == CLASSIFICATION_VERDICT
        clean = ArtifactValidationReport(total=5, passed=5)
        assert clean.classification == CLASSIFICATION_VERDICT

    @pytest.mark.parametrize(
        "report",
        [
            ArtifactValidationReport.not_declared(boundary=True),
            ArtifactValidationReport.unsupported(
                artifact_format="parquet", schema_source="model", reason="no mapping"
            ),
            ArtifactValidationReport.absent(reason="artifact not found"),
        ],
        ids=["not_declared", "unsupported", "absent"],
    )
    def test_the_non_scan_outcomes_are_artifact_unverifiable(
        self, report: ArtifactValidationReport
    ) -> None:
        assert report.classification == CLASSIFICATION_ARTIFACT_UNVERIFIABLE

    def test_a_plumbing_failure_is_validator_broken(self) -> None:
        """Every plumbing failure degrades to ``absent``, so the outcome alone
        cannot separate "the artifact was not there" from "we fell over"."""
        broken = ArtifactValidationReport.absent(
            reason="validator raised: RuntimeError", validator_broken=True
        )
        honest = ArtifactValidationReport.absent(reason="artifact not found")
        assert broken.outcome == honest.outcome
        assert broken.classification == CLASSIFICATION_VALIDATOR_BROKEN
        assert honest.classification == CLASSIFICATION_ARTIFACT_UNVERIFIABLE

    def test_the_wrapper_marks_its_own_guard_rails(self) -> None:
        """Not a hand-written flag at the call site: the wrapper's `_plugin_broken`
        is the one constructor, so a new guard rail cannot forget the axis."""

        class _NotASource:
            pass

        from application_sdk.validation.wrapper import validate_artifact

        report = validate_artifact(Path("/nonexistent"), _NotASource())  # type: ignore[arg-type]
        assert report.classification == CLASSIFICATION_VALIDATOR_BROKEN


class TestEnforceable:
    """Which outcomes a posture is allowed to block on."""

    def test_a_flagged_verdict_is_enforceable(self) -> None:
        assert _flagged_report().enforceable is True

    def test_a_clean_verdict_is_not(self) -> None:
        assert ArtifactValidationReport(total=5, passed=5).enforceable is False

    def test_validator_broken_is_never_enforceable(self) -> None:
        """The fail-open axis: a defect in the SDK's check may not fail a run."""
        assert (
            ArtifactValidationReport.absent(
                reason="validator raised", validator_broken=True
            ).enforceable
            is False
        )

    def test_undeclared_blocks_only_on_a_public_boundary(self) -> None:
        """ADR-0020 makes declaration optional on internal ``@task`` contracts, so
        blocking there would enforce a rule the ADR deliberately did not make."""
        assert ArtifactValidationReport.not_declared(boundary=True).enforceable is True
        assert (
            ArtifactValidationReport.not_declared(boundary=False).enforceable is False
        )

    @pytest.mark.parametrize("boundary", [True, False])
    def test_a_declared_artifact_that_could_not_be_proved_is_enforceable(
        self, boundary: bool
    ) -> None:
        """Unlike ``not_declared``, these mean the app *did* declare something and
        the hand-off could not be proved against it — internally too, because the
        app asked for the check by declaring."""
        assert (
            ArtifactValidationReport.unsupported(
                artifact_format="parquet",
                schema_source="model",
                reason="no mapping",
                boundary=boundary,
            ).enforceable
            is True
        )
        assert (
            ArtifactValidationReport.absent(
                reason="artifact not found", boundary=boundary
            ).enforceable
            is True
        )


class TestEnforcementIsOneDecision:
    """``blocked`` and ``would_block`` are two returns from one expression."""

    def test_hard_blocks_what_soft_would_block(self) -> None:
        report = _flagged_report()
        assert artifact_enforcement(report, enforce=True) == ENFORCEMENT_BLOCKED
        assert artifact_enforcement(report, enforce=False) == ENFORCEMENT_WOULD_BLOCK

    @pytest.mark.parametrize("enforce", [True, False])
    def test_an_unblockable_outcome_reports_neither_in_either_mode(
        self, enforce: bool
    ) -> None:
        clean = ArtifactValidationReport(total=5, passed=5)
        assert artifact_enforcement(clean, enforce=enforce) == ENFORCEMENT_NONE
        broken = ArtifactValidationReport.absent(
            reason="validator raised", validator_broken=True
        )
        assert artifact_enforcement(broken, enforce=enforce) == ENFORCEMENT_NONE

    def test_the_two_postures_agree_on_the_blockable_set(self) -> None:
        """The forecast property: whatever soft says would_block is exactly what
        hard blocks. If these ever diverge, the measured false-positive rate stops
        describing the behaviour being graduated to (FND-694)."""
        reports = [
            _flagged_report(),
            ArtifactValidationReport(total=5, passed=5),
            ArtifactValidationReport.not_declared(boundary=True),
            ArtifactValidationReport.not_declared(boundary=False),
            ArtifactValidationReport.unsupported(
                artifact_format="parquet", schema_source="model", reason="no mapping"
            ),
            ArtifactValidationReport.absent(reason="gone"),
            ArtifactValidationReport.absent(reason="broke", validator_broken=True),
        ]
        would = [
            r
            for r in reports
            if artifact_enforcement(r, enforce=False) == ENFORCEMENT_WOULD_BLOCK
        ]
        did = [
            r
            for r in reports
            if artifact_enforcement(r, enforce=True) == ENFORCEMENT_BLOCKED
        ]
        assert would == did


# ---------------------------------------------------------------------------
# The hook under a posture
# ---------------------------------------------------------------------------


class TestSoftNeverBlocks:
    @pytest.mark.asyncio
    async def test_a_flagged_handoff_proceeds_and_reports_would_block(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _PostureOut(
            queries=FileReference(local_path=_stringified_timestamp(tmp_path))
        )
        rows = await _run(out, enforce=False)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "flagged"
        assert rows[0][ARTIFACT_ENFORCEMENT_KEY] == ENFORCEMENT_WOULD_BLOCK
        assert rows[0][ARTIFACT_MODE_KEY] == MODE_SOFT
        assert rows[0][ARTIFACT_CLASSIFICATION_KEY] == CLASSIFICATION_VERDICT

    @pytest.mark.asyncio
    async def test_soft_is_the_default(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        """No ``enforce`` argument at all still proceeds — an app gets blocking
        only by asking for it."""
        out = _PostureOut(
            queries=FileReference(local_path=_stringified_timestamp(tmp_path))
        )
        rows = await _run(out)
        assert rows[0][ARTIFACT_ENFORCEMENT_KEY] == ENFORCEMENT_WOULD_BLOCK

    @pytest.mark.asyncio
    async def test_a_clean_handoff_reports_no_enforcement(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _PostureOut(queries=FileReference(local_path=_clean(tmp_path)))
        rows = await _run(out, enforce=False)
        assert rows[0]["outcome"] == "clean"
        assert rows[0][ARTIFACT_ENFORCEMENT_KEY] == ENFORCEMENT_NONE


class TestHardBlocks:
    @pytest.mark.asyncio
    async def test_a_flagged_handoff_fails_the_activity(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _PostureOut(
            queries=FileReference(local_path=_stringified_timestamp(tmp_path))
        )
        with pytest.raises(ArtifactValidationBlockedError) as excinfo:
            await _run(out, enforce=True)
        assert "queries" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_the_row_says_blocked_before_the_raise(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        """The outcome event is emitted first, so a blocked hand-off is queryable
        rather than only visible as a red activity."""
        out = _PostureOut(
            queries=FileReference(local_path=_stringified_timestamp(tmp_path))
        )
        with patch.object(interceptor_module, "logger") as logger:
            with pytest.raises(ArtifactValidationBlockedError):
                await validate_artifacts(
                    out,
                    side=ARTIFACT_SIDE_HANDOFF,
                    boundary_contracts=frozenset({_PostureOut}),
                    enforce=True,
                )
            rows = _rows(logger)
        assert len(rows) == 1
        assert rows[0][ARTIFACT_ENFORCEMENT_KEY] == ENFORCEMENT_BLOCKED
        assert rows[0][ARTIFACT_MODE_KEY] == MODE_HARD

    @pytest.mark.asyncio
    async def test_a_clean_handoff_still_proceeds(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _PostureOut(queries=FileReference(local_path=_clean(tmp_path)))
        rows = await _run(out, enforce=True)
        assert rows[0]["outcome"] == "clean"

    @pytest.mark.asyncio
    async def test_the_error_is_attributable(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        """Typed, so the field, the side and the disagreement reach the red
        activity pane and the Automation Engine without parsing a message."""
        out = _PostureOut(
            queries=FileReference(local_path=_stringified_timestamp(tmp_path))
        )
        with pytest.raises(ArtifactValidationBlockedError) as excinfo:
            await _run(out, enforce=True, app_name="myapp", entrypoint="run")
        err = excinfo.value
        assert type(err).category is FailureCategory.DATA_INTEGRITY
        assert type(err).audience is Audience.APP_OWNER
        assert err.effective_retryable is False
        assert err.app_name == "myapp"
        assert "queries" in (err.location or "")
        assert ARTIFACT_SIDE_HANDOFF in (err.location or "")
        assert "run" in (err.expectation or "")
        assert "START_TIME" in (err.observed or "")
        assert "ATLAN_ARTIFACT_VALIDATION_MODE" in (err.suggested_action or "")

    @pytest.mark.asyncio
    async def test_the_ingest_side_blocks_too_and_names_itself(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        """A hard app is asking for provable hand-offs in both directions. The
        side is on the error because the two want opposite fixes: at handoff this
        task wrote the artifact, at ingest it was handed one."""
        out = _PostureOut(
            queries=FileReference(local_path=_stringified_timestamp(tmp_path))
        )
        with pytest.raises(ArtifactValidationBlockedError) as excinfo:
            await _run(out, side=ARTIFACT_SIDE_INGEST, enforce=True)
        assert ARTIFACT_SIDE_INGEST in (excinfo.value.location or "")

    @pytest.mark.asyncio
    async def test_an_undeclared_internal_artifact_never_blocks(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _PostureInternalOut(
            scratch=FileReference(local_path=_clean(tmp_path, "scratch.json"))
        )
        rows = await _run(out, enforce=True, boundary_contracts=frozenset())
        assert rows[0]["outcome"] == "not_declared"
        assert rows[0][ARTIFACT_ENFORCEMENT_KEY] == ENFORCEMENT_NONE

    @pytest.mark.asyncio
    async def test_an_undeclared_boundary_artifact_does_block(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        class _UndeclaredBoundaryOut(Output, allow_unbounded_fields=True):
            nowhere: FileReference | None = None

        out = _UndeclaredBoundaryOut(
            nowhere=FileReference(local_path=_clean(tmp_path, "nowhere.json"))
        )
        with pytest.raises(ArtifactValidationBlockedError):
            await _run(
                out,
                enforce=True,
                boundary_contracts=frozenset({_UndeclaredBoundaryOut}),
            )


class TestBrokenValidatorAlwaysFailsOpen:
    """The axis that is never subject to mode."""

    @pytest.mark.parametrize("enforce", [True, False])
    @pytest.mark.asyncio
    async def test_a_validator_crash_proceeds_in_either_posture(
        self, enforce: bool, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _PostureOut(queries=FileReference(local_path=_clean(tmp_path)))
        with patch.object(
            interceptor_module,
            "_report_for",
            side_effect=RuntimeError("the check itself fell over"),
        ):
            rows = await _run(out, enforce=enforce)
        assert len(rows) == 1
        assert rows[0][ARTIFACT_CLASSIFICATION_KEY] == CLASSIFICATION_VALIDATOR_BROKEN
        assert rows[0][ARTIFACT_ENFORCEMENT_KEY] == ENFORCEMENT_NONE

    @pytest.mark.asyncio
    async def test_a_crash_emits_its_classification_rather_than_going_quiet(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _PostureOut(queries=FileReference(local_path=_clean(tmp_path)))
        with patch.object(
            interceptor_module, "_report_for", side_effect=RuntimeError("boom")
        ):
            rows = await _run(out, enforce=True)
        assert rows[0]["outcome"] == "absent"
        assert rows[0][ARTIFACT_CLASSIFICATION_KEY] == CLASSIFICATION_VALIDATOR_BROKEN

    @pytest.mark.parametrize("enforce", [True, False])
    @pytest.mark.asyncio
    async def test_an_unreadable_declaration_never_fails_the_handoff(
        self, enforce: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed ``artifact_schemas.json`` is the SDK's read failing, not
        evidence about the artifact — so it must not block, even in hard mode.

        Routed through a *durable* reference on purpose: with no ``local_path``
        there is nothing to scan, so the declaration is read by the interceptor's
        own ``_no_local_artifact`` rather than by the wrapper. Both branches see
        the same unreadable file, and if only one of them classified it
        ``validator_broken`` then whether a hard-mode activity failed would depend
        on whether the artifact happened to have been materialised.
        """
        generated = tmp_path / "generated"
        generated.mkdir()
        # An envelope version this loader does not understand: present and
        # well-formed JSON, but a shape nobody promised — `_parse_schemas` raises
        # ArtifactDeclarationError rather than best-effort parsing it.
        (generated / "artifact_schemas.json").write_bytes(
            orjson.dumps({"version": 999, "schemas": _DECLARED})
        )
        monkeypatch.setattr(
            "application_sdk.validation.sources.CONTRACT_GENERATED_DIR", str(generated)
        )
        out = _PostureOut(
            queries=FileReference(
                storage_path="artifacts/queries.json", is_durable=True
            )
        )
        rows = await _run(out, enforce=enforce)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "absent"
        assert rows[0][ARTIFACT_CLASSIFICATION_KEY] == CLASSIFICATION_VALIDATOR_BROKEN
        assert rows[0][ARTIFACT_ENFORCEMENT_KEY] == ENFORCEMENT_NONE

    def test_both_declaration_readers_classify_it_the_same_way(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The interceptor and the wrapper each read the declaration on their own
        path. They must agree, or blocking becomes a function of materialisation."""
        generated = tmp_path / "generated"
        generated.mkdir()
        (generated / "artifact_schemas.json").write_bytes(
            orjson.dumps({"version": 999, "schemas": _DECLARED})
        )
        monkeypatch.setattr(
            "application_sdk.validation.sources.CONTRACT_GENERATED_DIR", str(generated)
        )
        from application_sdk.validation.sources import ContractSource
        from application_sdk.validation.wrapper import validate_artifact

        source = ContractSource(field="queries", entrypoint="")
        via_interceptor = interceptor_module._no_local_artifact(source, boundary=True)
        via_wrapper = validate_artifact(tmp_path / "queries.json", source)

        assert (
            via_interceptor.classification
            == via_wrapper.classification
            == CLASSIFICATION_VALIDATOR_BROKEN
        )
        assert via_interceptor.enforceable is via_wrapper.enforceable is False

    @pytest.mark.parametrize("enforce", [True, False])
    @pytest.mark.asyncio
    async def test_a_broken_walk_never_fails_the_handoff(
        self, enforce: bool, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _PostureOut(queries=FileReference(local_path=_clean(tmp_path)))
        with patch.object(
            interceptor_module, "_walk", side_effect=RuntimeError("walk broke")
        ):
            rows = await _run(out, enforce=enforce)
        assert rows == []


class TestDeclaredButUnreadableStillBlocks:
    """The other half of the ``_no_local_artifact`` split.

    A declared artifact with no local copy is ``absent`` for a reason that is not
    the SDK's fault, so a hard posture blocks on it — the app asked for a check it
    could not be given. Only the *declaration* being unreadable fails open.
    """

    @pytest.mark.asyncio
    async def test_a_durable_reference_with_a_readable_declaration_blocks(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _PostureOut(
            queries=FileReference(
                storage_path="artifacts/queries.json", is_durable=True
            )
        )
        with pytest.raises(ArtifactValidationBlockedError):
            await _run(out, enforce=True)

    @pytest.mark.asyncio
    async def test_and_reports_would_block_under_soft(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _PostureOut(
            queries=FileReference(
                storage_path="artifacts/queries.json", is_durable=True
            )
        )
        rows = await _run(out, enforce=False)
        assert rows[0]["outcome"] == "absent"
        assert (
            rows[0][ARTIFACT_CLASSIFICATION_KEY] == CLASSIFICATION_ARTIFACT_UNVERIFIABLE
        )
        assert rows[0][ARTIFACT_ENFORCEMENT_KEY] == ENFORCEMENT_WOULD_BLOCK


class TestEveryReferenceEmitsBeforeAnyBlock:
    @pytest.mark.asyncio
    async def test_a_flagged_first_reference_does_not_silence_the_second(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        """Blocking early would make an app's row count depend on which artifact
        failed — and that count is the denominator FND-694 reads."""
        out = _TwoRefsOut(
            first=FileReference(
                local_path=_ndjson(tmp_path, "one.json", [{"WRONG": "x"}])
            ),
            second=FileReference(
                local_path=_ndjson(tmp_path, "two.json", [{"QUERY_ID": "q"}])
            ),
        )
        with patch.object(interceptor_module, "logger") as logger:
            with pytest.raises(ArtifactValidationBlockedError) as excinfo:
                await validate_artifacts(
                    out,
                    side=ARTIFACT_SIDE_HANDOFF,
                    boundary_contracts=frozenset({_TwoRefsOut}),
                    enforce=True,
                )
            rows = _rows(logger)
        assert [r["artifact_field"] for r in rows] == ["first", "second"]
        assert rows[0][ARTIFACT_ENFORCEMENT_KEY] == ENFORCEMENT_BLOCKED
        assert rows[1][ARTIFACT_ENFORCEMENT_KEY] == ENFORCEMENT_NONE
        # One block, so no "and N more" tail.
        assert "more blocked reference" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_several_blocked_references_are_counted_in_the_message(
        self, tmp_path: Path, generated_dir: Path
    ) -> None:
        out = _TwoRefsOut(
            first=FileReference(
                local_path=_ndjson(tmp_path, "one.json", [{"WRONG": "x"}])
            ),
            second=FileReference(
                local_path=_ndjson(tmp_path, "two.json", [{"ALSO_WRONG": "y"}])
            ),
        )
        with pytest.raises(ArtifactValidationBlockedError) as excinfo:
            await _run(out, enforce=True)
        message = str(excinfo.value)
        assert "'first'" in message
        assert "1 more blocked reference" in message


class TestKillSwitchOutranksThePosture:
    @pytest.mark.asyncio
    async def test_nothing_runs_and_nothing_blocks_when_disabled(
        self, tmp_path: Path, generated_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.VALIDATE_ARTIFACTS", False, raising=False
        )
        out = _PostureOut(
            queries=FileReference(local_path=_stringified_timestamp(tmp_path))
        )
        assert await _run(out, enforce=True) == []


# ---------------------------------------------------------------------------
# The posture event
# ---------------------------------------------------------------------------


class TestPostureEvent:
    """The boot-time denominator: which apps believe they are validating."""

    @pytest.mark.parametrize(
        ("enforce", "enabled", "expected"),
        [
            (True, True, MODE_HARD),
            (False, True, MODE_SOFT),
            (True, False, MODE_OFF),
            (False, False, MODE_OFF),
        ],
    )
    def test_emits_the_resolved_mode(
        self, enforce: bool, enabled: bool, expected: str
    ) -> None:
        with patch.object(interceptor_module, "logger") as logger:
            log_artifact_validation_posture("myapp", enforce=enforce, enabled=enabled)
        call = logger.info.call_args
        assert call.args[0] == ARTIFACT_VALIDATION_POSTURE_EVENT
        assert call.kwargs["app_name"] == "myapp"
        assert call.kwargs[ARTIFACT_MODE_KEY] == expected

    def test_emitted_for_soft_apps_too(self) -> None:
        """A hard-only row gives no denominator: an app whose tasks hand off no
        artifacts emits no outcome row at all, so from outcomes alone it is
        indistinguishable from one that is not registered."""
        with patch.object(interceptor_module, "logger") as logger:
            log_artifact_validation_posture("softapp", enforce=False, enabled=True)
        logger.info.assert_called_once()

    def test_the_name_is_pinned_in_the_registry(self) -> None:
        assert ARTIFACT_VALIDATION_POSTURE_EVENT == "Artifact validation posture"
        assert ARTIFACT_VALIDATION_POSTURE_EVENT in OUTCOME_EVENT_NAMES

    def test_the_mode_key_reaches_otlp(self) -> None:
        """Two of the three parts of the event contract are easy to get right and
        fail silently; this is the third."""
        assert ARTIFACT_MODE_KEY in _KNOWN_EXTRA_KEYS
        assert ARTIFACT_CLASSIFICATION_KEY in _KNOWN_EXTRA_KEYS
        assert ARTIFACT_ENFORCEMENT_KEY in _KNOWN_EXTRA_KEYS
