"""Tests for the canonical task-queue derivation (FND-195).

The point of the module under test is that the worker and the served manifest
agree byte-for-byte. The tests are written the same way: the divergence cases
from FND-195 are asserted as *equality between the two paths*, not as two
independently hard-coded expectations, so a future change to the rule can't
satisfy one side and quietly break the other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from application_sdk.common.task_queue import (
    derive_task_queue,
    resolve_manifest_tokens,
    task_queue_from_env,
)
from application_sdk.main import _derive_task_queue


@dataclass(frozen=True)
class DeploymentEnv:
    """The two env vars the queue name is derived from."""

    application_name: str | None
    deployment_name: str | None

    def apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key, value in (
            ("ATLAN_APPLICATION_NAME", self.application_name),
            ("ATLAN_DEPLOYMENT_NAME", self.deployment_name),
        ):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)


def _manifest_bytes(task_queue: str, app_name: str = "{app_name}") -> bytes:
    """A minimal DAG manifest in the shape the contract toolkit emits."""
    return json.dumps(
        {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "app_name": app_name,
                    "inputs": {"args": {"app_name": app_name}},
                    "task_queue": task_queue,
                }
            },
        }
    ).encode()


def _served_queue(raw: bytes) -> str:
    return json.loads(raw)["dag"]["extract"]["task_queue"]


class TestDeriveTaskQueue:
    def test_app_and_deployment_are_prefixed(self) -> None:
        assert derive_task_queue("dbt", "prod") == "atlan-dbt-prod"

    def test_app_only_is_bare_and_unprefixed(self) -> None:
        """DISTR-834 shipped a double prefix by pre-prefixing an already-prefixed
        value; the bare shape is deliberate, not an oversight."""
        assert derive_task_queue("dbt", None) == "dbt"
        assert derive_task_queue("dbt", "") == "dbt"

    def test_no_app_name_invents_nothing(self) -> None:
        assert derive_task_queue(None, "prod") is None
        assert derive_task_queue("", "prod") is None
        assert derive_task_queue("   ", "prod") is None

    def test_whitespace_is_stripped(self) -> None:
        assert derive_task_queue(" dbt ", " prod ") == "atlan-dbt-prod"


class TestWorkerFallback:
    """``_derive_task_queue`` adds exactly one thing to the shared rule."""

    def test_delegates_when_app_name_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        assert _derive_task_queue("pkg.apps:DbtApp") == task_queue_from_env()

    def test_class_name_queue_only_when_app_name_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        DeploymentEnv(None, "prod").apply(monkeypatch)
        assert task_queue_from_env() is None
        assert _derive_task_queue("pkg.apps:DbtApp") == "dbt-app-queue"


class TestPrecedenceIndependentOracle:
    """Hard-coded expectations for every precedence row, no shared oracle.

    The equality tests above prove worker and manifest *agree*; they cannot
    catch both sides drifting together, because ``_derive_task_queue`` and the
    resolver's fallback both bottom out in ``task_queue_from_env``. These rows
    assert the concrete queue each precedence case must produce, so a drift in
    the shared rule fails here even when the two paths still agree.
    """

    @pytest.mark.parametrize(
        ("env", "expected_queue"),
        [
            # Both set → prefixed pair.
            pytest.param(DeploymentEnv("dbt", "prod"), "atlan-dbt-prod", id="both"),
            # App only → bare, unprefixed (never atlan-dbt-default).
            pytest.param(DeploymentEnv("dbt", None), "dbt", id="app-only"),
            # Deployment only → no name determinable.
            pytest.param(DeploymentEnv(None, "prod"), None, id="deployment-only"),
            # Neither → no name determinable.
            pytest.param(DeploymentEnv(None, None), None, id="neither"),
        ],
    )
    def test_env_derivation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env: DeploymentEnv,
        expected_queue: str | None,
    ) -> None:
        env.apply(monkeypatch)
        assert task_queue_from_env() == expected_queue
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-dbt-{deployment_name}")
        )
        assert resolution.task_queue == expected_queue

    def test_explicit_override_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``ATLAN_TASK_QUEUE`` / ``--task-queue`` is authoritative over any
        env-derived value — the row re-derivation can never reproduce."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-dbt-{deployment_name}"),
            task_queue="atlan-dbt-ci",
        )
        assert resolution.task_queue == "atlan-dbt-ci"
        assert _served_queue(resolution.raw) == "atlan-dbt-ci"

    def test_env_beats_registered_name_in_the_template(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A baked name that no longer matches the deployment's env is corrected
        to the env queue — the divergence the stamp exists to close."""
        DeploymentEnv("dbt-v3", "prod").apply(monkeypatch)
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-dbt-{deployment_name}"), app_name="dbt"
        )
        assert resolution.task_queue == "atlan-dbt-v3-prod"
        assert _served_queue(resolution.raw) == "atlan-dbt-v3-prod"


class TestManifestAgreesWithWorker:
    """The three rows of FND-195's divergence table."""

    @pytest.mark.parametrize(
        "env",
        [
            pytest.param(DeploymentEnv("dbt", "prod"), id="both-set"),
            pytest.param(DeploymentEnv("dbt", None), id="no-deployment-name"),
        ],
    )
    @pytest.mark.parametrize(
        "template",
        [
            pytest.param("atlan-{app_name}-{deployment_name}", id="unbaked"),
            pytest.param("atlan-dbt-{deployment_name}", id="toolkit-baked"),
        ],
    )
    def test_served_queue_equals_worker_queue(
        self, monkeypatch: pytest.MonkeyPatch, env: DeploymentEnv, template: str
    ) -> None:
        env.apply(monkeypatch)
        resolution = resolve_manifest_tokens(_manifest_bytes(template))
        assert _served_queue(resolution.raw) == _derive_task_queue("pkg.apps:DbtApp")
        assert resolution.task_queue == _derive_task_queue("pkg.apps:DbtApp")
        assert not resolution.unresolved_app_name

    def test_no_deployment_name_collapses_to_the_bare_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Row 2 of the table: token-filling gave ``atlan-dbt-default`` while the
        worker polled a bare ``dbt``. Resolving the template as a unit fixes it."""
        DeploymentEnv("dbt", None).apply(monkeypatch)
        raw = resolve_manifest_tokens(
            _manifest_bytes("atlan-dbt-{deployment_name}")
        ).raw
        assert _served_queue(raw) == "dbt"

    def test_unset_app_name_leaves_the_token_visible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Row 3: the manifest must not manufacture ``atlan-default-prod``, which
        reads as legitimate and reproduces the CONNECT-183 hang."""
        DeploymentEnv(None, "prod").apply(monkeypatch)
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-{app_name}-{deployment_name}")
        )
        assert resolution.task_queue is None
        assert resolution.unresolved_app_name
        served = _served_queue(resolution.raw)
        assert "{app_name}" in served
        assert "default" not in served

    def test_a_known_worker_queue_is_stamped_even_with_no_app_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stamping the queue the worker actually polls is not invention, so it
        applies even here — but the log-identity token stays visible, because
        there is still nothing truthful to fill it with."""
        DeploymentEnv(None, "prod").apply(monkeypatch)
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-{app_name}-{deployment_name}"),
            task_queue="some-app-queue",
        )
        assert _served_queue(resolution.raw) == "some-app-queue"
        assert resolution.unresolved_app_name
        assert "{app_name}" in resolution.raw.decode()


class TestWorkerQueueIsStampedNotRederived:
    """The handler knows the queue its worker polls, so it stamps that value.

    This is the part re-derivation cannot reach: an explicit ``ATLAN_TASK_QUEUE``
    override, or a baked name that no longer matches the deployment's env.
    """

    def test_explicit_worker_queue_wins_over_the_env_derivation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-dbt-{deployment_name}"),
            task_queue="atlan-dbt-ci",
        )
        assert _served_queue(resolution.raw) == "atlan-dbt-ci"
        assert resolution.task_queue == "atlan-dbt-ci"

    def test_baked_name_disagreeing_with_env_is_corrected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manifest baked as ``dbt`` on a deployment renamed to ``dbt-v3`` used
        to serve ``atlan-dbt-prod`` while the worker polled ``atlan-dbt-v3-prod``
        — plausible-looking, and dead."""
        DeploymentEnv("dbt-v3", "prod").apply(monkeypatch)
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-dbt-{deployment_name}"), app_name="dbt"
        )
        assert _served_queue(resolution.raw) == "atlan-dbt-v3-prod"

    def test_registered_name_resolves_the_queue_with_no_env_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local dev: no deployment env, so the worker polls ``{ClassName}-queue``.
        The manifest now advertises that same queue instead of a manufactured
        ``atlan-minimal-local`` nothing polls."""
        DeploymentEnv(None, None).apply(monkeypatch)
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-minimal-{deployment_name}"),
            task_queue="minimal-app-queue",
            app_name="minimal",
        )
        assert _served_queue(resolution.raw) == "minimal-app-queue"


class TestManifestResidualTokens:
    def test_app_name_log_identity_is_substituted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HYP-1954: a literal ``{app_name}`` in ``inputs.args`` reached
        observability and stripped failure attribution."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        resolution = resolve_manifest_tokens(_manifest_bytes("atlan-dbt-prod"))
        node = json.loads(resolution.raw)["dag"]["extract"]
        assert node["app_name"] == "dbt"
        assert node["inputs"]["args"]["app_name"] == "dbt"
        # Filled, but still reported: the committed manifest is stale.
        assert resolution.had_app_name_token
        assert not resolution.unresolved_app_name

    def test_registered_name_fills_the_token_over_the_env_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The registered name is what the toolkit would have baked, and what the
        Workflow Center's log filter matches (HYP-1678)."""
        DeploymentEnv("dbt-v3", "prod").apply(monkeypatch)
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-dbt-prod"), app_name="dbt"
        )
        assert resolution.app_name == "dbt"
        assert json.loads(resolution.raw)["dag"]["extract"]["app_name"] == "dbt"

    def test_baked_app_name_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The toolkit's baked value is what Workflow Center's log filter matches
        (HYP-1678); it is never overwritten with the env value."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        raw = resolve_manifest_tokens(
            _manifest_bytes("atlan-dbt-prod", app_name="baked-name")
        ).raw
        assert json.loads(raw)["dag"]["extract"]["app_name"] == "baked-name"

    def test_another_apps_queue_is_token_filled_not_normalised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Platform nodes legitimately dispatch to other apps' queues
        (``atlan-publish-{deployment_name}``). Those are not this app's queue and
        must be left alone rather than rewritten to it."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        raw = resolve_manifest_tokens(
            _manifest_bytes("atlan-publish-{deployment_name}")
        ).raw
        assert _served_queue(raw) == "atlan-publish-prod"

    def test_deployment_fallback_applies_only_to_residual_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback keeps the pre-FND-195 behaviour of a bare
        ``{deployment_name}``, but must not leak into queue derivation — that
        manufactured segment is the divergence itself."""
        DeploymentEnv("dbt", None).apply(monkeypatch)
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-dbt-{deployment_name}"),
            deployment_fallback="prod-deploy",
        )
        assert _served_queue(resolution.raw) == "dbt"
        assert resolution.task_queue == "dbt"

    def test_residual_deployment_token_uses_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        DeploymentEnv("dbt", None).apply(monkeypatch)
        raw = resolve_manifest_tokens(
            _manifest_bytes("{deployment_name}-queue"),
            deployment_fallback="prod-deploy",
        ).raw
        assert _served_queue(raw) == "prod-deploy-queue"

    def test_env_deployment_wins_over_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        raw = resolve_manifest_tokens(
            _manifest_bytes("{deployment_name}-queue"),
            deployment_fallback="ignored",
        ).raw
        assert _served_queue(raw) == "prod-queue"


class TestQueueStampIsFieldAware:
    """The stamp owns ``task_queue`` fields only, and runs after the token fills.

    Both findings below are the same class — unscoped byte substitution — that
    the whole-manifest replace used to commit: it mutated the stamped queue with
    the later token passes, and it re-pointed bytes that were never this app's
    queue to begin with.
    """

    def test_a_configured_queue_containing_token_text_is_stamped_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit ``ATLAN_TASK_QUEUE`` / ``--task-queue`` override may
        legitimately contain literal token text (``custom-{deployment_name}-queue``).
        The residual passes must not rewrite it post-stamp into a queue no worker
        polls while ``resolution.task_queue`` still reports the original."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        resolution = resolve_manifest_tokens(
            _manifest_bytes("atlan-dbt-{deployment_name}"),
            task_queue="custom-{deployment_name}-queue",
        )
        assert _served_queue(resolution.raw) == "custom-{deployment_name}-queue"
        assert resolution.task_queue == "custom-{deployment_name}-queue"

    def test_a_foreign_app_node_matching_the_template_is_not_repointed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DAG node that dispatches to *another* app's queue keeps it. The byte
        substitutor matched ``atlan-{candidate}-{deployment_name}`` anywhere in the
        manifest — including a foreign node whose baked queue equals this app's env
        name — and re-pointed it at this app's worker."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        manifest = json.dumps(
            {
                "execution_mode": "dag",
                "dag": {
                    "extract": {
                        "activity_name": "execute_workflow",
                        "task_queue": "atlan-dbt-{deployment_name}",
                    },
                    # A node that hands off to the dbt app's own queue, baked.
                    # It matches a candidate but is not this node's own stamp.
                    "notify_dbt": {
                        "activity_name": "execute_workflow",
                        "task_queue": "atlan-dbt-prod",
                    },
                },
            }
        ).encode()
        served = json.loads(resolve_manifest_tokens(manifest).raw)["dag"]
        assert served["extract"]["task_queue"] == "atlan-dbt-prod"
        # The foreign node is token-filled like any residual, never normalised
        # to this app's stamped queue — and crucially never double-stamped.
        assert served["notify_dbt"]["task_queue"] == "atlan-dbt-prod"

    def test_template_text_in_a_description_is_not_rewritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Free-text fields are not queues. A description mentioning the template
        must pass through untouched even though it matches the byte pattern."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        manifest = json.dumps(
            {
                "execution_mode": "dag",
                "dag": {
                    "extract": {
                        "activity_name": "execute_workflow",
                        "description": "polls atlan-dbt-{deployment_name} for work",
                        "task_queue": "atlan-dbt-{deployment_name}",
                    }
                },
            }
        ).encode()
        node = json.loads(resolve_manifest_tokens(manifest).raw)["dag"]["extract"]
        assert node["task_queue"] == "atlan-dbt-prod"
        # The description is a residual field, not a queue: its {deployment_name}
        # is token-filled like any other, but it is never *stamped* — had it been
        # treated as a queue it would also have been rewritten when the stamped
        # queue differed from the template fill (see the override test above).
        assert node["description"] == "polls atlan-dbt-prod for work"

    def test_template_text_in_a_description_is_never_stamped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stronger form: when the stamped queue differs from the template fill
        (an explicit override), a description holding the template must not track
        the stamp — only a real ``task_queue`` field may."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        manifest = json.dumps(
            {
                "execution_mode": "dag",
                "dag": {
                    "extract": {
                        "activity_name": "execute_workflow",
                        "description": "polls atlan-dbt-{deployment_name} for work",
                        "task_queue": "atlan-dbt-{deployment_name}",
                    }
                },
            }
        ).encode()
        node = json.loads(
            resolve_manifest_tokens(manifest, task_queue="atlan-dbt-ci").raw
        )["dag"]["extract"]
        # The real queue field is stamped with the override...
        assert node["task_queue"] == "atlan-dbt-ci"
        # ...but the description is only token-filled, never re-pointed at it.
        assert node["description"] == "polls atlan-dbt-prod for work"

    def test_queue_fields_are_stamped_at_any_depth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real manifests carry the queue at ``dag.<node>.task_queue``,
        ``dag.<node>.inputs.task_queue``, and top level; the field-aware stamp
        reaches all of them, not just the first nesting level."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        manifest = json.dumps(
            {
                "execution_mode": "dag",
                "task_queue": "atlan-dbt-{deployment_name}",
                "dag": {
                    "extract": {
                        "activity_name": "execute_workflow",
                        "task_queue": "atlan-dbt-{deployment_name}",
                        "inputs": {"task_queue": "atlan-dbt-{deployment_name}"},
                    }
                },
            }
        ).encode()
        served = json.loads(resolve_manifest_tokens(manifest).raw)
        assert served["task_queue"] == "atlan-dbt-prod"
        assert served["dag"]["extract"]["task_queue"] == "atlan-dbt-prod"
        assert served["dag"]["extract"]["inputs"]["task_queue"] == "atlan-dbt-prod"

    def test_unparseable_manifest_is_served_unstamped_and_logged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manifest too malformed to parse is served back byte-for-byte and
        logged at ERROR, not byte-substituted. Byte substitution cannot scope the
        residual fills away from a stamped queue carrying literal token text, so
        it re-imports the defect this module removes — a loud failure beats a
        silently wrong queue."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        malformed = b'{"dag": {"extract": {"task_queue": "atlan-dbt-{deployment_name}"'
        with patch("application_sdk.common.task_queue.logger.error") as mock_error:
            resolution = resolve_manifest_tokens(malformed)
        assert resolution.raw == malformed
        assert resolution.task_queue == "atlan-dbt-prod"
        mock_error.assert_called_once()
        message = mock_error.call_args.args[0] % mock_error.call_args.args[1:]
        assert "does not parse as JSON" in message

    def test_scalar_root_manifest_is_served_unstamped_with_accurate_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid JSON with a scalar root (``null``, a number, a string) has no
        object to walk, so it takes the same unstamped path — but the log must
        not claim a syntax error that isn't there. The message distinguishes
        \"scalar root\" from \"does not parse\" so an operator is not sent
        hunting a malformed manifest that is in fact well-formed JSON."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        with patch("application_sdk.common.task_queue.logger.error") as mock_error:
            resolution = resolve_manifest_tokens(b"null")
        assert resolution.raw == b"null"
        mock_error.assert_called_once()
        message = mock_error.call_args.args[0] % mock_error.call_args.args[1:]
        assert "scalar root" in message
        assert "does not parse as JSON" not in message

    def test_unparseable_manifest_with_token_carrying_override_is_not_mutated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: an override carrying literal token text through the
        malformed path must survive untouched. The old byte fallback stamped the
        template and then ran the residual fills over the result, rewriting the
        stamped ``custom-{deployment_name}-queue`` to ``custom-prod-queue`` while
        ``resolution.task_queue`` still reported the original — the served
        manifest and the worker disagreed on the queue. Serving unstamped removes
        the divergence: the bytes never change, so there is no second answer."""
        DeploymentEnv("dbt", "prod").apply(monkeypatch)
        malformed = b'{"task_queue": "atlan-dbt-{deployment_name}"'
        resolution = resolve_manifest_tokens(
            malformed, task_queue="custom-{deployment_name}-queue"
        )
        assert resolution.raw == malformed
        assert b"custom-prod-queue" not in resolution.raw
        assert resolution.task_queue == "custom-{deployment_name}-queue"
