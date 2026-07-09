"""AppManifest carries the AE WorkflowOwnership field (SYSTEM/USER)."""

from application_sdk.handler.manifest import AppManifest


def test_ownership_defaults_to_none_when_absent():
    m = AppManifest(execution_mode="automation-engine", dag={})
    assert m.ownership is None
    # Omitted from serialized output by default so the orchestration layer
    # applies its is_system_app-derived default.
    assert "ownership" not in m.model_dump(exclude_none=True)


def test_ownership_roundtrips_when_set():
    m = AppManifest.model_validate(
        {"execution_mode": "automation-engine", "dag": {}, "ownership": "SYSTEM"}
    )
    assert m.ownership == "SYSTEM"
    assert '"ownership":"SYSTEM"' in m.model_dump_json().replace(" ", "")
