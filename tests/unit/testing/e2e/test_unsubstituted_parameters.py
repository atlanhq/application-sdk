"""Unit tests for the unresolved-mustache detector on the AE submit path.

The harness submits ``{{credentialGuid}}`` as a deliberate literal and relies
on AE turning the request's ``payload[]`` credential block into a real GUID. If
AE answers 2xx with a run_id but skips that substitution, nothing in the
harness notices: the run is polled normally and the literal only surfaces a
poll interval later as a worker-side ``[AAF-CRD-005] Invalid credential GUID``
on the extract node, which reads as a connector defect rather than a
control-plane one. These tests pin the detector that names it at submit time.

See FND-402 / FND-656.
"""

from __future__ import annotations

from unittest.mock import patch

from application_sdk.testing.e2e.client import AEWorkflowClient
from application_sdk.testing.harness.automation_engine.retry import (
    unsubstituted_parameter_tokens as _unsubstituted_parameter_tokens,
)


def _make_client() -> AEWorkflowClient:
    return AEWorkflowClient(
        tenant_url="https://tenant.example.com",
        api_token="tok-test",
    )


def _submit_response(*parameters: dict) -> dict:
    """An AE submit response echoing the Argo parameter block."""
    return {
        "data": {"run_id": "run-xyz"},
        "spec": {
            "templates": [
                {
                    "name": "main",
                    "dag": {
                        "tasks": [
                            {
                                "name": "run",
                                "arguments": {"parameters": list(parameters)},
                            }
                        ]
                    },
                }
            ]
        },
    }


class TestUnsubstitutedParameterTokens:
    def test_finds_unresolved_credential_guid(self):
        found = _unsubstituted_parameter_tokens(
            _submit_response({"name": "credential-guid", "value": "{{credentialGuid}}"})
        )
        assert found == {"credential-guid": "credentialGuid"}

    def test_resolved_guid_is_not_reported(self):
        found = _unsubstituted_parameter_tokens(
            _submit_response(
                {
                    "name": "credential-guid",
                    "value": "a1b2c3d4-dead-beef-0000-111122223333",
                }
            )
        )
        assert found == {}

    def test_reports_every_unresolved_parameter(self):
        found = _unsubstituted_parameter_tokens(
            _submit_response(
                {"name": "credential-guid", "value": "{{credentialGuid}}"},
                {"name": "include-filter", "value": '{"^dev$": ["^public$"]}'},
                {"name": "temp-table-regex", "value": "{{temp-table-regex}}"},
            )
        )
        assert found == {
            "credential-guid": "credentialGuid",
            "temp-table-regex": "temp-table-regex",
        }

    def test_json_blob_value_is_not_a_false_positive(self):
        """A value that merely contains braces is not failed substitution."""
        found = _unsubstituted_parameter_tokens(
            _submit_response(
                # the redshift crawler's real include_filter shape
                {
                    "name": "include-filter",
                    "value": '{"^dev$": ["^public$", "^workflows$"]}',
                },
                {"name": "connection", "value": '{"typeName": "Connection"}'},
                {"name": "exclude-filter", "value": "{}"},
            )
        )
        assert found == {}

    def test_embedded_token_is_not_reported(self):
        """Only a value that is EXACTLY one token counts as unresolved."""
        found = _unsubstituted_parameter_tokens(
            _submit_response(
                {"name": "compiled-url", "value": "redshift://host/{{db}}?ssl=true"}
            )
        )
        assert found == {}

    def test_surrounding_whitespace_still_matches(self):
        found = _unsubstituted_parameter_tokens(
            _submit_response(
                {"name": "credential-guid", "value": " {{credentialGuid}} "}
            )
        )
        assert found == {"credential-guid": "credentialGuid"}

    def test_tolerates_unexpected_shapes(self):
        """The response shape is undocumented — the walker must never raise."""
        for body in (
            {},
            {"data": None},
            {"spec": []},
            {"a": [1, "x", None, {"b": {}}]},
        ):
            assert _unsubstituted_parameter_tokens(body) == {}

    def test_ignores_non_parameter_name_value_pairs(self):
        """A name/value dict whose value is not a bare token is skipped."""
        found = _unsubstituted_parameter_tokens(
            {"name": "some-node", "value": {"nested": "dict"}}
        )
        assert found == {}


class TestSubmitWarnsOnUnsubstituted:
    def _payload(self) -> dict:
        return {
            "payload": [
                {
                    "parameter": "credentialGuid",
                    "type": "credential",
                    "body": {"name": "default-redshift-1234-1"},
                }
            ]
        }

    @staticmethod
    def _rendered(warn_mock) -> str:
        """The warning as it reaches the log, args interpolated."""
        assert warn_mock.called, "expected a warning about unresolved parameters"
        fmt, *args = warn_mock.call_args.args
        return fmt % tuple(args)

    def test_submit_warns_but_still_returns_run_id(self):
        """The detector is diagnostic only — it must not change the outcome."""
        client = _make_client()
        resp = _submit_response(
            {"name": "credential-guid", "value": "{{credentialGuid}}"}
        )
        with (
            patch.object(client._ae, "_request", return_value=(200, resp)),
            patch(
                "application_sdk.testing.harness.automation_engine.client.logger"
            ) as log,
        ):
            run_id = client.submit_workflow(self._payload(), retries=0)
        assert run_id == "run-xyz"
        rendered = self._rendered(log.warning)
        assert "AAF-CRD-005" in rendered
        assert "credential-guid" in rendered
        # names the fault's owner, so the reader does not chase the connector
        assert "control-plane" in rendered

    def test_no_warning_on_a_fully_resolved_submit(self):
        client = _make_client()
        resp = _submit_response(
            {"name": "credential-guid", "value": "a1b2c3d4-dead-beef-0000-111122223333"}
        )
        with (
            patch.object(client._ae, "_request", return_value=(200, resp)),
            patch(
                "application_sdk.testing.harness.automation_engine.client.logger"
            ) as log,
        ):
            run_id = client.submit_workflow(self._payload(), retries=0)
        assert run_id == "run-xyz"
        assert not log.warning.called

    def test_warning_never_prints_parameter_values(self):
        """Argo parameter values can carry source credentials — names only."""
        client = _make_client()
        resp = _submit_response(
            {"name": "credential-guid", "value": "{{credentialGuid}}"},
            {"name": "agent-json", "value": "s3cr3t-should-never-be-logged"},
        )
        with (
            patch.object(client._ae, "_request", return_value=(200, resp)),
            patch(
                "application_sdk.testing.harness.automation_engine.client.logger"
            ) as log,
        ):
            client.submit_workflow(self._payload(), retries=0)
        assert "s3cr3t-should-never-be-logged" not in self._rendered(log.warning)

    def test_logs_response_shape_even_when_nothing_is_unresolved(self):
        """The shape is the unknown — it must be logged on EVERY accepted submit.

        Regression guard: the first cut logged the shape only inside the
        warning, so a real tenant whose submit response carries no parameter
        block at all produced total silence and no way to learn why.
        """
        client = _make_client()
        resp = _submit_response(
            {"name": "credential-guid", "value": "a1b2c3d4-dead-beef-0000-111122223333"}
        )
        with (
            patch.object(client._ae, "_request", return_value=(200, resp)),
            patch(
                "application_sdk.testing.harness.automation_engine.client.logger"
            ) as log,
        ):
            client.submit_workflow(self._payload(), retries=0)
        assert not log.warning.called
        fmt, *args = log.info.call_args.args
        rendered = fmt % tuple(args)
        assert "response keys=" in rendered
        assert "'data'" in rendered and "'spec'" in rendered

    def test_response_shape_log_never_prints_values(self):
        client = _make_client()
        resp = {"data": {"run_id": "run-xyz", "token": "s3cr3t-value"}}
        with (
            patch.object(client._ae, "_request", return_value=(200, resp)),
            patch(
                "application_sdk.testing.harness.automation_engine.client.logger"
            ) as log,
        ):
            client.submit_workflow(self._payload(), retries=0)
        fmt, *args = log.info.call_args.args
        rendered = fmt % tuple(args)
        assert "s3cr3t-value" not in rendered
        # the KEY is fine to name; only the value must not appear
        assert "token" in rendered
