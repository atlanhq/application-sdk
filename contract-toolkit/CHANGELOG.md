# Changelog

All notable changes to this project are documented here.

Release notes are generated from [conventional commit](https://www.conventionalcommits.org/)
messages. Every merge to `main` that touches `contract-toolkit/` files auto-creates or
updates a release PR — see `.github/workflows/contract-toolkit-release.yml`.

## [0.10.5] - 2026-06-03

### Bug fixes

- update QI contract (#1971) ([a6345f6](https://github.com/atlanhq/application-sdk/commit/a6345f6))
- emit lint-clean generated input contracts (#1967) ([c1dcdde](https://github.com/atlanhq/application-sdk/commit/c1dcdde))

## [0.10.4] - 2026-05-29

### Bug fixes

- corrects generated output for credentials (#1928) ([17da250](https://github.com/atlanhq/application-sdk/commit/17da250))

## [0.10.3] - 2026-05-28

### Bug fixes

- support credential checkbox and auto-derive auth-type hidden (#1873) ([944c290](https://github.com/atlanhq/application-sdk/commit/944c290))

## [0.10.2] - 2026-05-27

### Bug fixes

- add current_state_enabled + current_state_via_app_enabled to PublishNode and PublishStep (#1866) ([318af06](https://github.com/atlanhq/application-sdk/commit/318af06))

## [0.10.1] - 2026-05-22

### Bug fixes

- default connection cache to true/false for PublishNode/LineagePublishNode (#1850) ([dd49f65](https://github.com/atlanhq/application-sdk/commit/dd49f65))
- default splitDeployment and keda.enabled to true (#1837) ([772fa4c](https://github.com/atlanhq/application-sdk/commit/772fa4c))

## [0.10.0] - 2026-05-21

### Features

- Consolidate to single `App.pkl` template; legacy modules (`NativeApp.pkl`, `NativeAppBundle.pkl`, `Renderers.pkl`) retained as frozen reference material ([#1814](https://github.com/atlanhq/application-sdk/pull/1814))

### Bug fixes

- Read `.default` uniformly in `getInputPyField` so optional fields with explicit defaults render correctly in generated `_input.py` ([#1803](https://github.com/atlanhq/application-sdk/pull/1803))

## [0.9.5] - 2026-05-07

### Features

- Widen QueryIntelligenceNode ignoreOrphans/indirectLineage to Boolean|String? (#60) ([#60](https://github.com/atlanhq/app-contract-toolkit/pull/60))

## [0.9.4] - 2026-05-06

### Breaking changes

- Remove QueryIntelligenceNode per-workflow QI args (#59) ([#59](https://github.com/atlanhq/app-contract-toolkit/pull/59))

## [0.9.3] - 2026-05-04

### Bug fixes

- Omit popularity lake provider by default (#58) ([#58](https://github.com/atlanhq/app-contract-toolkit/pull/58))

## [0.9.2] - 2026-05-01

### Bug fixes

- Align apitree object defaults with input validation (#55) ([#55](https://github.com/atlanhq/app-contract-toolkit/pull/55))

### Documentation

- Add toolkit feature workflow skill (#54) ([#54](https://github.com/atlanhq/app-contract-toolkit/pull/54))

### Features

- Increase startToCloseTimeoutSeconds limit to 72h (#56) ([#56](https://github.com/atlanhq/app-contract-toolkit/pull/56))

## [0.9.1] - 2026-04-30

### Bug fixes

- Prefer enable-tags for tag pipeline default (#52) ([#52](https://github.com/atlanhq/app-contract-toolkit/pull/52))
- Format GitHub release notes (#41) ([#41](https://github.com/atlanhq/app-contract-toolkit/pull/41))
- Trigger pages build after release publish (#40) ([#40](https://github.com/atlanhq/app-contract-toolkit/pull/40))

### Documentation

- Update make-contract toolkit guidance (#42) ([#42](https://github.com/atlanhq/app-contract-toolkit/pull/42))

### Features

- Add built-in node error handling knobs (#51) ([#51](https://github.com/atlanhq/app-contract-toolkit/pull/51))
- Add FieldSpec rows UI hint (#50) ([#50](https://github.com/atlanhq/app-contract-toolkit/pull/50))
- Add tag pipeline publish defaults (#49) ([#49](https://github.com/atlanhq/app-contract-toolkit/pull/49))
- Allow built-in activity display name overrides (#48) ([#48](https://github.com/atlanhq/app-contract-toolkit/pull/48))
- Make lineage storage args optional (#47) ([#47](https://github.com/atlanhq/app-contract-toolkit/pull/47))

## [0.9.0] - 2026-04-27

### Bug fixes

- Emit connection ref flag only under ui (#38) ([#38](https://github.com/atlanhq/app-contract-toolkit/pull/38))
- Default native manifest args to top-level

### Documentation

- Refresh agent guides and contract skills (#39) ([#39](https://github.com/atlanhq/app-contract-toolkit/pull/39))

### Features

- **(NativeApp)** Generate AppInputContract extending ExtractionInput (#35) ([#35](https://github.com/atlanhq/app-contract-toolkit/pull/35))
- Add connection ref input widget (#37) ([#37](https://github.com/atlanhq/app-contract-toolkit/pull/37))
- Add popularity DAG node template (#36) ([#36](https://github.com/atlanhq/app-contract-toolkit/pull/36))
- Add multi-entrypoint bundle contracts (#34) ([#34](https://github.com/atlanhq/app-contract-toolkit/pull/34))
- Add Power BI widget support HYP-765 (#32) ([#32](https://github.com/atlanhq/app-contract-toolkit/pull/32))
- Accept Config.UIElement inside AuthOption.extraFields via NamedWidget (#25) ([#25](https://github.com/atlanhq/app-contract-toolkit/pull/25))
- Add publish input and executor controls (#31) ([#31](https://github.com/atlanhq/app-contract-toolkit/pull/31))
- Add explicit dependency conditions (#30) ([#30](https://github.com/atlanhq/app-contract-toolkit/pull/30))
- Add lineage publish node (#29) ([#29](https://github.com/atlanhq/app-contract-toolkit/pull/29))

## [0.7.1] - 2026-04-24

### Features

- **(NativeApp)** AdvancedJDBCUrlGroup credential primitive + manual-only releases (#28) ([#28](https://github.com/atlanhq/app-contract-toolkit/pull/28))

## [0.7.0] - 2026-04-23

### Features

- Add query intelligence node and credential config controls (#27) ([#27](https://github.com/atlanhq/app-contract-toolkit/pull/27))

## [0.6.0] - 2026-04-22

### Bug fixes

- Extract release body from CHANGELOG.md instead of --latest

### Features

- **(NativeApp)** Promote DAGNode.dependsOn to multi-parent fan-in (#26) ([#26](https://github.com/atlanhq/app-contract-toolkit/pull/26))

## [0.5.0] - 2026-04-22

### CI & tooling

- Grant pull-requests:read to the release job (#24) ([#24](https://github.com/atlanhq/app-contract-toolkit/pull/24))
- Add grouped release notes + CHANGELOG.md via git-cliff (#23) ([#23](https://github.com/atlanhq/app-contract-toolkit/pull/23))

### Features

- Add widget features for Fivetran AE integration (HYP-724) (#22) ([#22](https://github.com/atlanhq/app-contract-toolkit/pull/22))

## [0.4.1] - 2026-04-21

### Bug fixes

- Emit snake_case keys in manifest JSON instead of kebab-case (#21) ([#21](https://github.com/atlanhq/app-contract-toolkit/pull/21))

### Chores

- Rewrite contract-review skill and add post-PR self-review rule

## [0.4.0] - 2026-04-20

### CI & tooling

- Add workflow_dispatch + idempotent bump to release workflow
- Pull SDK from main branch for import test

### Chores

- Restructure contract-review skill and sync AGENTS.md

### Documentation

- Revert SAP_S4_HANA/SAP_ECC category to erp

### Features

- Add compound-filter conditional credentials (#19) ([#19](https://github.com/atlanhq/app-contract-toolkit/pull/19))

## [0.3.14] - 2026-04-20

### CI & tooling

- Add PR validation workflow and fix release versioning (#15) ([#15](https://github.com/atlanhq/app-contract-toolkit/pull/15))

### Chores

- Add contract-review skill (#17) ([#17](https://github.com/atlanhq/app-contract-toolkit/pull/17))
- Add CODEOWNERS requiring maintainer review on all PRs

### Features

- Add SAP S/4 HANA connector with conditional credential fields

## [0.3.13] - 2026-04-14

### HYP-311

- Multi-manifest support — taskQueuePrefix, workflowType auto-kebab, ConditionalInput fix (#14) ([#14](https://github.com/atlanhq/app-contract-toolkit/pull/14))

## [0.3.12] - 2026-04-13

### Features

- Add error_handling support to DAGNode (#12) ([#12](https://github.com/atlanhq/app-contract-toolkit/pull/12))

## [0.3.11] - 2026-04-09

### Bug fixes

- Emit default at property level for BooleanInput (#11) ([#11](https://github.com/atlanhq/app-contract-toolkit/pull/11))

## [0.3.10] - 2026-04-06

### Features

- Add InputRepeater, Evaluate widgets and field-level attributes [APP-1229] (#9) ([#9](https://github.com/atlanhq/app-contract-toolkit/pull/9))

## [0.3.9] - 2026-04-02

### Features

- Support conditional widget switching and fix AgentSelector default key

## [0.3.8] - 2026-03-31

### Features

- Expose app_name, app_id, argo_workflow_slug in manifest extract and publish nodes

## [0.3.7] - 2026-03-31

### Features

- Expose app_name, app_id, argo_workflow_slug in manifest extract node (#7) ([#7](https://github.com/atlanhq/app-contract-toolkit/pull/7))

## [0.3.6] - 2026-03-27

### Bug fixes

- Rename generated output from input.py to _input.py

## [0.3.5] - 2026-03-27

### Features

- Auto-emit allow_unbounded_fields for SDK v3 payload safety

## [0.3.4] - 2026-03-25

### Bug fixes

- Hoist topLevelFormFields out of mapping block to fix Pkl scope bug

## [0.3.3] - 2026-03-25

### Features

- Add flatManifestArgs flag to emit all params top-level in manifest args

## [0.3.2] - 2026-03-25

### Bug fixes

- Gate publish node on hasPublishStep, rename hasLoader

## [0.3.1] - 2026-03-24

### Chores

- Synced main

### Other changes

- Rename hasPublishApp to usePublishApp

## [0.2.11] - 2026-03-24

### Chores

- Remove dead import, stale output dir, update docs

## [0.2.10] - 2026-03-24

### Chores

- Regenerate example input.py files for Pydantic BaseModel

### Features

- Add databaseExcludePatterns to Config.SqlTree
- Make hasPublishApp default to true (opt-out instead of opt-in)

## [0.2.9] - 2026-03-23

### Bug fixes

- Convert hyphens to underscores in generated Python field names

## [0.2.8] - 2026-03-23

### Features

- Add monte-carlo example exercising isHidden, connectionCategories, APITree.desc

## [0.2.7] - 2026-03-23

### Bug fixes

- Add connectionCategories to Widget class, add trino example

## [0.2.6] - 2026-03-23

### Features

- Add FieldSpec.isHidden, ConnectionSelector.connectionCategories, APITree.desc

## [0.2.5] - 2026-03-23

### Documentation

- Update reference with latest version discovery and fix textarea widget mapping

## [0.2.4] - 2026-03-23

### Bug fixes

- Map textarea FieldType to correct frontend widget name

## [0.2.3] - 2026-03-23

### Bug fixes

- Move TextInput default value to property level
- Remove atlan- prefix from workflowConfigName default

### CI & tooling

- Merge version bump into release workflow
- Fix release workflow artifact loss and add auto version bump
- Switch package distribution to GitHub Pages
- Add GitHub Actions workflow for PKL package releases

### Documentation

- Improve documentation site layout and usability
- Add comprehensive reference documentation

### Features

- Generate Input.py dataclass from app contract

### Other changes

- Migrate generated Input class from dataclass to Pydantic BaseModel
- Replace hasLoader with hasPublishApp and slim down publish fields
- Fix generated Input class to always use AppInputContract name

## [0.2.0] - 2026-03-12

### Bug fixes

- App contract missing properties

### Documentation

- Add AGENTS.md
- Rewrite README and CLAUDE.md for native app contract system

### Features

- Package v0.2.0 for PKL registry distribution
- FieldSpec-based credential model aligned with SDK credential system
- Add PublishNode, remove includePublishNode flag
- Add NativeApp.pkl for temporal-native app contracts
