# contract-toolkit: Pkl app-contract toolkit
- Flag: `src/*.pkl` changes (`NativeApp.pkl`, `NativeAppBundle.pkl`, `Config.pkl`, `AgentConfig.pkl`) with no `examples/` update or no update to both `README.md` and `docs/reference.md` (`read_diff`).
- Flag: default output changed for existing apps when not a deliberate bug fix (new behaviour should be opt-in); no default-behaviour assertion in `tests/*_test.pkl`.
- Flag: broken value flow: config field → `manifest.json` arg → `_input.py` field; credential field → credential config → UI conditionals; typed node property → manifest node args. A `{{params.x}}` with no generated field, or a required value with no producer/consumer.
- Flag: generated `AppInputContract` not extending `ExtractionInput`, or a field dropped/renamed/retyped; top-level `extraction_method`/`credential_guid`/`agent_json` removed or nested (preflight gate).
- Flag: new native credential logic in legacy `Credential.pkl` or rendering in `Renderers.pkl`; raw `DAGNode` where a typed node (`PublishNode`, `LineageNode`) fits.
- Flag: a fix or invariant with no Pkl test; a `pkl` version restated instead of read from `application_sdk/pkl_version.py`.
- Flag: internal consumer repo names, paths or branches in public docs.
- Severity: high for silent breakage of generated artifacts or the UI/manifest/SDK contract; medium for missing tests/docs/examples; low otherwise.
