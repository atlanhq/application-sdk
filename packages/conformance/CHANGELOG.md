# Changelog

All notable changes to `atlan-application-sdk-conformance` are documented here.

## [0.25.0] - 2026-08-31

### Features

- let one suite run N entrypoint DAGs against one connection (#3569) ([da1ed37](https://github.com/atlanhq/application-sdk/commit/da1ed37))
- P023 covers the data-scale blocking calls the fleet actually makes (#3566) ([720ac0f](https://github.com/atlanhq/application-sdk/commit/720ac0f))
- publish the persistent-artifacts seam and enforce it (CONNECT-1275) (#3523) ([d854e00](https://github.com/atlanhq/application-sdk/commit/d854e00))
- ban locale-dependent text file IO (P046) (FND-924) (#3466) ([67c81f3](https://github.com/atlanhq/application-sdk/commit/67c81f3))
- scaffold application_sdk/testing/harness/ (FND-238) (#3433) ([997b697](https://github.com/atlanhq/application-sdk/commit/997b697))

### Bug fixes

- atomic FileReference materialisation and per-path lock (CONNECT-1126) (#3520) ([24ba8fd](https://github.com/atlanhq/application-sdk/commit/24ba8fd))
- gate conformance SARIF upload on code-scanning availability (FND-1149, FND-1150) (#3553) ([38b10af](https://github.com/atlanhq/application-sdk/commit/38b10af))
- B005 false-positive shapes (#3477) ([7ef432e](https://github.com/atlanhq/application-sdk/commit/7ef432e))
- I003 false-positive shapes (#3478) ([1e701b3](https://github.com/atlanhq/application-sdk/commit/1e701b3))
- L004 false-positive shapes (#3474) ([678595d](https://github.com/atlanhq/application-sdk/commit/678595d))
- B005 false positives from bare-class-name ledger identity (#3471) ([47c5f84](https://github.com/atlanhq/application-sdk/commit/47c5f84))
- P014 false-positive shapes (#3472) ([13a63fc](https://github.com/atlanhq/application-sdk/commit/13a63fc))
- log gate blocks as ERROR with audience classification (FND-901) (#3492) ([22d2095](https://github.com/atlanhq/application-sdk/commit/22d2095))
- P013 false-positive shapes (#3475) ([38ba374](https://github.com/atlanhq/application-sdk/commit/38ba374))
- alarm when a lock refusal outlives the reaper (FND-909) (#3505) ([a24b5ae](https://github.com/atlanhq/application-sdk/commit/a24b5ae))
- P003 reads codes held in a module-level constant (#3473) ([13ce559](https://github.com/atlanhq/application-sdk/commit/13ce559))
- D011 accepts an upper-bound-only specifier (#3468) ([f579514](https://github.com/atlanhq/application-sdk/commit/f579514))
- update github/codeql-action digest to cdf488f (#3442) ([5fc6dbb](https://github.com/atlanhq/application-sdk/commit/5fc6dbb))

### Other changes

- chore(deps): update dependency ruff to v0.16.5 (#3518) ([5e9d9a7](https://github.com/atlanhq/application-sdk/commit/5e9d9a7))
- chore(testing): freeze testing/full_dag and make it visible to B001 (FND-245) (#3485) ([ce69c07](https://github.com/atlanhq/application-sdk/commit/ce69c07))
- refactor(testing): re-express BaseE2ETest over the shared harness, in place (FND-225) (#3476) ([4b50db6](https://github.com/atlanhq/application-sdk/commit/4b50db6))
- refactor(testing): split e2e client.py into async Atlas and AE readers (FND-242) (#3438) ([75da7fb](https://github.com/atlanhq/application-sdk/commit/75da7fb))

## [0.24.0] - 2026-08-25

### Features

- K017 — a declared artifact schema its own writer contradicts (FND-693) (#3414) ([06b431d](https://github.com/atlanhq/application-sdk/commit/06b431d))
- accept legacy Temporal workflow types as inbound-only aliases (#3132) ([bd90391](https://github.com/atlanhq/application-sdk/commit/bd90391))

### Bug fixes

- report Release Gate on merge-queue branches (FND-851) (#3418) ([2f8c97c](https://github.com/atlanhq/application-sdk/commit/2f8c97c))

## [0.23.0] - 2026-08-25

### Features

- require an artifact schema on every entrypoint FileReference (FND-687) (#3389) ([e68790c](https://github.com/atlanhq/application-sdk/commit/e68790c))
- add D011, a BLOCK rule for the conformance suite's own dependency contract (FND-419) (#3373) ([6df3953](https://github.com/atlanhq/application-sdk/commit/6df3953))
- native --rule runner scoping + per-rule doc files (#3372) ([fc4b272](https://github.com/atlanhq/application-sdk/commit/fc4b272))
- support multi-entrypoint apps across the e2e and integration tiers (#3286) ([3af3bba](https://github.com/atlanhq/application-sdk/commit/3af3bba))

### Bug fixes

- tell a stale bounded-lock refusal from a broken build (FND-782) (#3384) ([38f933f](https://github.com/atlanhq/application-sdk/commit/38f933f))
- E010 no longer fires on enumerate/subscript-inspected gather results (#3364) ([7c57e9b](https://github.com/atlanhq/application-sdk/commit/7c57e9b))
- update dependency ruff to v0.16.4 (#3365) ([d4caaa3](https://github.com/atlanhq/application-sdk/commit/d4caaa3))
- remove obsolete P041 rule (#3367) ([ca58617](https://github.com/atlanhq/application-sdk/commit/ca58617))

### Other changes

- chore(contract-toolkit): release v0.22.1 (#3304) ([afad725](https://github.com/atlanhq/application-sdk/commit/afad725))

## [0.22.0] - 2026-08-21

### Features

- rule-level remediation scoping, docker-build gate, apply-unverifiable mode (FND-18) (#3264) ([374cb11](https://github.com/atlanhq/application-sdk/commit/374cb11))

### Bug fixes

- make the vendored bootstrap scaffold lint-clean in any caller (FND-445) (#3350) ([255b529](https://github.com/atlanhq/application-sdk/commit/255b529))
- update github/codeql-action digest to db488dd (#3348) ([dc7a493](https://github.com/atlanhq/application-sdk/commit/dc7a493))
- fail the tenant lease where the missing grant can be named (FND-702) (#3341) ([fafa0b4](https://github.com/atlanhq/application-sdk/commit/fafa0b4))
- stop bootstrap --resync dropping per-repo CI config (FND-604) (#3283) ([e1adc0f](https://github.com/atlanhq/application-sdk/commit/e1adc0f))
- pin the B006 ledger remedy to the checker's own version (FND-607) (#3284) ([88a2419](https://github.com/atlanhq/application-sdk/commit/88a2419))

## [0.21.0] - 2026-08-18

### Features

- make application_sdk.errors the enforced public error contract (CONNECT-970) (#3266) ([0e8508e](https://github.com/atlanhq/application-sdk/commit/0e8508e))
- add P044 for app-level prefix transfers (FND-433) (#3263) ([bf1287e](https://github.com/atlanhq/application-sdk/commit/bf1287e))

### Bug fixes

- resolve D003 import names from the target repo's env (FND-433) (#3260) ([a290191](https://github.com/atlanhq/application-sdk/commit/a290191))

### Other changes

- chore(contract-toolkit): release v0.22.0 (#3261) ([9a5bf21](https://github.com/atlanhq/application-sdk/commit/9a5bf21))

## [0.20.1] - 2026-08-17

### Bug fixes

- feed e2e evidence and cross-CSP coverage to the test-readiness scorecard (FND-33, FND-34) (#3245) ([752808d](https://github.com/atlanhq/application-sdk/commit/752808d))
- give the commits shim a real reusable, retire docstring-coverage (FND-381) (#3236) ([ca132f2](https://github.com/atlanhq/application-sdk/commit/ca132f2))

## [0.20.0] - 2026-08-17

### Features

- graduate P030 + seven WARN rules to BLOCK; customer-impact line required in every BLOCK rationale (FND-311) (#3169) ([34c541a](https://github.com/atlanhq/application-sdk/commit/34c541a))

## [0.19.1] - 2026-08-14

### Bug fixes

- let apps own their coverage floor and GHCR base opt-in (FND-361) (#3215) ([6bef6a9](https://github.com/atlanhq/application-sdk/commit/6bef6a9))

## [0.19.0] - 2026-08-14

### Features

- add bootstrap --resync for scaffold drift (FND-356) (#3210) ([42b5a53](https://github.com/atlanhq/application-sdk/commit/42b5a53))
- add granular 0-touch levers under --enforce (FND-347) (#3206) ([ee37002](https://github.com/atlanhq/application-sdk/commit/ee37002))
- lane-model fleet preset + per-PR delivered-deps in the fleet scanner (#3129) ([893c6ba](https://github.com/atlanhq/application-sdk/commit/893c6ba))

### Bug fixes

- update dependency @openprose/reactor-devtools to ^0.3.0 (#3167) ([17d2774](https://github.com/atlanhq/application-sdk/commit/17d2774))
- update github/codeql-action digest to ff2f1c6 (#3196) ([c79c926](https://github.com/atlanhq/application-sdk/commit/c79c926))
- make CI toolchain downloads survive a CDN outage (FND-287) (#3176) ([e28e9f8](https://github.com/atlanhq/application-sdk/commit/e28e9f8))

### Other changes

- refactor(runtime): move the offload and progress seams to a dependency-neutral substrate (FND-316) (#3189) ([55bda27](https://github.com/atlanhq/application-sdk/commit/55bda27))
- chore(contract-toolkit): release v0.21.1 (#3144) ([b6a467e](https://github.com/atlanhq/application-sdk/commit/b6a467e))
- chore(contract-toolkit): release v0.21.0 (#3126) ([f87e56b](https://github.com/atlanhq/application-sdk/commit/f87e56b))

## [0.18.0] - 2026-08-11

### Features

- add K014 requiring an explicit release_model in atlan.yaml (#3119) ([b831330](https://github.com/atlanhq/application-sdk/commit/b831330))
- add D010/P040/P041/B007/T020-T024 from the SDR fleet sweep (#2894) ([ff1d8d1](https://github.com/atlanhq/application-sdk/commit/ff1d8d1))
- add O005 UnresolvedAppNamePlaceholder (#3094) ([d4ff03d](https://github.com/atlanhq/application-sdk/commit/d4ff03d))
- add O006 DirectRocksdictImport rule (#3089) ([70fe46e](https://github.com/atlanhq/application-sdk/commit/70fe46e))
- K013 flags DAG nodes whose app_name disagrees with the app that runs them (#3056) ([9426832](https://github.com/atlanhq/application-sdk/commit/9426832))
- run every e2e suite against an AWS/Azure/GCP tenant matrix (#2997) ([338595e](https://github.com/atlanhq/application-sdk/commit/338595e))
- extend P023 to tree-scale filesystem work in async defs (#2988) ([a828e22](https://github.com/atlanhq/application-sdk/commit/a828e22))
- let bootstrap carry an app's pre-commit system deps (#2904) ([1beb7df](https://github.com/atlanhq/application-sdk/commit/1beb7df))
- promote L002 to block for app production code (#2902) ([6c16247](https://github.com/atlanhq/application-sdk/commit/6c16247))

### Bug fixes

- only re-run e2e when the e2e label is the one just added (#3065) ([5f6c52f](https://github.com/atlanhq/application-sdk/commit/5f6c52f))
- exempt redaction-boundary log calls from L004/E005 (#3032) ([2eeaa5e](https://github.com/atlanhq/application-sdk/commit/2eeaa5e))
- split conformance package bumps into a dedicated auto-merged Renovate PR (#3041) ([c4cec09](https://github.com/atlanhq/application-sdk/commit/c4cec09))
- update github/codeql-action digest to 5595cca (#2993) ([13a49e1](https://github.com/atlanhq/application-sdk/commit/13a49e1))
- update github/codeql-action digest to d1ba80a (#2967) ([aa5d5cf](https://github.com/atlanhq/application-sdk/commit/aa5d5cf))
- mirror Input.app_name in SDK contract base registry (#2964) ([e07ea56](https://github.com/atlanhq/application-sdk/commit/e07ea56))
- resolve log app_name per-entrypoint from workflow input (#2951) ([1f2b24c](https://github.com/atlanhq/application-sdk/commit/1f2b24c))
- update github/codeql-action digest to f205ea1 (#2944) ([907626f](https://github.com/atlanhq/application-sdk/commit/907626f))

### Other changes

- chore(contract-toolkit): release v0.20.0 (#3046) ([ba7e46c](https://github.com/atlanhq/application-sdk/commit/ba7e46c))
- chore(contract-toolkit): release v0.19.2 (#2959) ([7ddd1fa](https://github.com/atlanhq/application-sdk/commit/7ddd1fa))
- chore(contract-toolkit): release v0.19.1 (#2864) ([bf33c1c](https://github.com/atlanhq/application-sdk/commit/bf33c1c))

## [0.17.0] - 2026-07-23

### Features

- add T019 AsyncioTestLoopScopeUnset test-quality rule (#2859) ([c89a78c](https://github.com/atlanhq/application-sdk/commit/c89a78c))
- per-tier coverage scorecard + re-enable emission (#2858) ([0e6fe02](https://github.com/atlanhq/application-sdk/commit/0e6fe02))
- add T018 — integration tier deselected by addopts (#2857) ([993ff9f](https://github.com/atlanhq/application-sdk/commit/993ff9f))

### Other changes

- chore(deps): lock file maintenance (#2855) ([fa43824](https://github.com/atlanhq/application-sdk/commit/fa43824))
- chore(deps): lock file maintenance (#2846) ([70c6816](https://github.com/atlanhq/application-sdk/commit/70c6816))

## [0.16.0] - 2026-07-22

### Features

- detect silent auto-merge-not-armed PRs in fleet scanner (#2832) ([a22bd6b](https://github.com/atlanhq/application-sdk/commit/a22bd6b))
- add P037/P038/P039 SDR-readiness rules (#2824) ([19474a0](https://github.com/atlanhq/application-sdk/commit/19474a0))
- test-readiness scorecard generator + CI publish (#2819) ([b82292f](https://github.com/atlanhq/application-sdk/commit/b82292f))

### Bug fixes

- revert unpublished 0.16.0 bump and hand-edited changelog (#2842) ([cbe4f08](https://github.com/atlanhq/application-sdk/commit/cbe4f08))
- update actions/checkout action to v7 (#2836) ([72eda8e](https://github.com/atlanhq/application-sdk/commit/72eda8e))
- update conformance action templates (#2835) ([c3efc38](https://github.com/atlanhq/application-sdk/commit/c3efc38))
- P016 recognizes DAG-routed secondary entrypoints (route/card split) (#2825) ([c2d78ed](https://github.com/atlanhq/application-sdk/commit/c2d78ed))
- run upload asset validation in an isolated child process (CNCT-85) (#2769) ([143e2d3](https://github.com/atlanhq/application-sdk/commit/143e2d3))
- regenerate contract-toolkit conformance baseline on release (#2808) ([1775f65](https://github.com/atlanhq/application-sdk/commit/1775f65))

### Other changes

- chore(deps): lock file maintenance (#2840) ([ea352e0](https://github.com/atlanhq/application-sdk/commit/ea352e0))
- chore(deps): lock file maintenance (#2834) ([d7f8ba7](https://github.com/atlanhq/application-sdk/commit/d7f8ba7))
- chore(deps): lock file maintenance (#2827) ([c36f434](https://github.com/atlanhq/application-sdk/commit/c36f434))
- chore(deps): lock file maintenance (#2821) ([03e9aca](https://github.com/atlanhq/application-sdk/commit/03e9aca))
- chore(deps): lock file maintenance (#2816) ([41785f7](https://github.com/atlanhq/application-sdk/commit/41785f7))
- chore(deps): lock file maintenance (#2814) ([d5b3bd2](https://github.com/atlanhq/application-sdk/commit/d5b3bd2))
- chore(deps): lock file maintenance (#2797) ([ce5f1d7](https://github.com/atlanhq/application-sdk/commit/ce5f1d7))
- chore(deps): lock file maintenance (#2794) ([665a29f](https://github.com/atlanhq/application-sdk/commit/665a29f))

## [0.15.0] - 2026-07-20

### Features

- add K011/K012 release-readiness guards (#2756) ([98b66b0](https://github.com/atlanhq/application-sdk/commit/98b66b0))

### Bug fixes

- contract-toolkit conformance prose/rules → self-hosted runner (#2792) ([a8d86c2](https://github.com/atlanhq/application-sdk/commit/a8d86c2))
- stop shipping the renovate-pkl-sync glue to new repos (PR2 Stage 4b) (#2790) ([cac4e79](https://github.com/atlanhq/application-sdk/commit/cac4e79))
- K002 skips legacy-import check for Credential.pkl contracts (#2742) ([cf2ec85](https://github.com/atlanhq/application-sdk/commit/cf2ec85))

### Other changes

- chore(deps): lock file maintenance (#2786) ([7c58823](https://github.com/atlanhq/application-sdk/commit/7c58823))
- chore(deps): lock file maintenance (#2775) ([ddba13e](https://github.com/atlanhq/application-sdk/commit/ddba13e))
- chore(deps): lock file maintenance (#2772) ([7a17675](https://github.com/atlanhq/application-sdk/commit/7a17675))
- chore(deps): lock file maintenance (#2771) ([e38d6a1](https://github.com/atlanhq/application-sdk/commit/e38d6a1))
- chore(deps): lock file maintenance (#2755) ([b22cade](https://github.com/atlanhq/application-sdk/commit/b22cade))
- chore(deps): lock file maintenance (#2752) ([d88272f](https://github.com/atlanhq/application-sdk/commit/d88272f))
- chore(deps): lock file maintenance (#2750) ([cb70168](https://github.com/atlanhq/application-sdk/commit/cb70168))
- chore(deps): lock file maintenance (#2748) ([2ef8474](https://github.com/atlanhq/application-sdk/commit/2ef8474))
- chore(deps): lock file maintenance (#2740) ([afcd63d](https://github.com/atlanhq/application-sdk/commit/afcd63d))
- chore(deps): lock file maintenance (#2730) ([25f8c08](https://github.com/atlanhq/application-sdk/commit/25f8c08))

## [0.14.0] - 2026-07-15

### Features

- deprecate BaseSDRIntegrationTest; T003 flags it fleet-wide (#2721) ([d828a4f](https://github.com/atlanhq/application-sdk/commit/d828a4f))
- preflight-gate rules P032-P035 (BLDX-1545) (#2665) ([013d486](https://github.com/atlanhq/application-sdk/commit/013d486))
- add T016+T017 e2e queue-isolation rules (#2698) ([010e121](https://github.com/atlanhq/application-sdk/commit/010e121))
- migrate cross-repo call sites to atlan-app-fleet App token (#2635) ([44b9323](https://github.com/atlanhq/application-sdk/commit/44b9323))
- SDR-QA gap coverage — obs upload + per-type asset counts (#2609) ([929cf18](https://github.com/atlanhq/application-sdk/commit/929cf18))

### Bug fixes

- P028 no longer flags object-store keys embedding a qualifiedName (#2719) ([eed14f6](https://github.com/atlanhq/application-sdk/commit/eed14f6))
- accept agent-mode e2e test as T002 SDR coverage (#2696) ([60c7c19](https://github.com/atlanhq/application-sdk/commit/60c7c19))
- E004 exempts broad excepts that re-raise with the trace intact (#2650) ([aab37f1](https://github.com/atlanhq/application-sdk/commit/aab37f1))
- D003 recognises SQLAlchemy dialect-string drivers as used (#2649) ([7993db5](https://github.com/atlanhq/application-sdk/commit/7993db5))
- migrate renovate-pkl-sync.yaml to App token, fix bootstrap template (#2686) ([d45e8fb](https://github.com/atlanhq/application-sdk/commit/d45e8fb))
- matrix the e2e suite (one job per file); retire xdist opt-in (#2683) ([6574e71](https://github.com/atlanhq/application-sdk/commit/6574e71))
- opt-in pytest-xdist parallelism for the e2e suite (#2668) ([37a32d4](https://github.com/atlanhq/application-sdk/commit/37a32d4))
- recognise project-local _assert_* helpers in T005/T007 (#2644) ([961389e](https://github.com/atlanhq/application-sdk/commit/961389e))

### Other changes

- chore(deps): lock file maintenance (#2726) ([d686133](https://github.com/atlanhq/application-sdk/commit/d686133))
- chore(deps): lock file maintenance (#2705) ([456ffe7](https://github.com/atlanhq/application-sdk/commit/456ffe7))
- chore(deps): lock file maintenance (#2704) ([5f2a533](https://github.com/atlanhq/application-sdk/commit/5f2a533))
- chore(deps): lock file maintenance (#2695) ([92c3626](https://github.com/atlanhq/application-sdk/commit/92c3626))
- chore(deps): lock file maintenance (#2693) ([88f9234](https://github.com/atlanhq/application-sdk/commit/88f9234))
- chore(deps): lock file maintenance (#2692) ([9d0e11d](https://github.com/atlanhq/application-sdk/commit/9d0e11d))
- chore(deps): lock file maintenance (#2688) ([68c35ba](https://github.com/atlanhq/application-sdk/commit/68c35ba))
- chore(deps): lock file maintenance (#2663) ([68d83bd](https://github.com/atlanhq/application-sdk/commit/68d83bd))
- chore(deps): lock file maintenance (#2662) ([f0af450](https://github.com/atlanhq/application-sdk/commit/f0af450))
- chore(deps): lock file maintenance (#2659) ([0644017](https://github.com/atlanhq/application-sdk/commit/0644017))
- chore(deps): lock file maintenance (#2658) ([ce86392](https://github.com/atlanhq/application-sdk/commit/ce86392))
- chore(deps): lock file maintenance (#2656) ([6a24b46](https://github.com/atlanhq/application-sdk/commit/6a24b46))
- chore(deps): lock file maintenance (#2653) ([3abd128](https://github.com/atlanhq/application-sdk/commit/3abd128))
- chore(deps): lock file maintenance (#2643) ([1417fb5](https://github.com/atlanhq/application-sdk/commit/1417fb5))
- chore(deps): lock file maintenance (#2629) ([c2ad002](https://github.com/atlanhq/application-sdk/commit/c2ad002))
- chore(deps): lock file maintenance (#2625) ([cc84eb1](https://github.com/atlanhq/application-sdk/commit/cc84eb1))
- chore(deps): lock file maintenance (#2620) ([6544f17](https://github.com/atlanhq/application-sdk/commit/6544f17))
- chore(deps): lock file maintenance (#2615) ([3c6fa00](https://github.com/atlanhq/application-sdk/commit/3c6fa00))
- chore(deps): lock file maintenance (#2612) ([dd2e092](https://github.com/atlanhq/application-sdk/commit/dd2e092))
- chore(deps): lock file maintenance (#2610) ([21617ca](https://github.com/atlanhq/application-sdk/commit/21617ca))
- chore(deps): lock file maintenance (#2601) ([4d7cd6b](https://github.com/atlanhq/application-sdk/commit/4d7cd6b))
- chore(deps): lock file maintenance (#2598) ([185e650](https://github.com/atlanhq/application-sdk/commit/185e650))
- chore(deps): lock file maintenance (#2595) ([50bd25a](https://github.com/atlanhq/application-sdk/commit/50bd25a))

## [0.13.0] - 2026-07-08

### Features

- default two-store: true in the tests.yaml bootstrap template (#2593) ([4b68806](https://github.com/atlanhq/application-sdk/commit/4b68806))
- add contract-toolkit hygiene rules and fix K002 false positive (#2584) ([8181eca](https://github.com/atlanhq/application-sdk/commit/8181eca))
- add K006 rule for manifest-vs-contract field validation (#2560) ([7114ceb](https://github.com/atlanhq/application-sdk/commit/7114ceb))

### Bug fixes

- remediate error-handling, logging, and optimization findings (#2573) ([460d7f2](https://github.com/atlanhq/application-sdk/commit/460d7f2))
- add opt-in two-store SDR posture to the full-DAG suite (#2586) ([bf87f36](https://github.com/atlanhq/application-sdk/commit/bf87f36))
- include S-series (security) in default remediation scope (#2570) ([269730b](https://github.com/atlanhq/application-sdk/commit/269730b))
- resolve B005/B006 contract fields across inheritance (#2557) ([ad9fddf](https://github.com/atlanhq/application-sdk/commit/ad9fddf))
- add P031 rule for shared-default-executor offload (#2556) ([76d92b2](https://github.com/atlanhq/application-sdk/commit/76d92b2))

### Other changes

- chore(deps): lock file maintenance (#2591) ([4736ccd](https://github.com/atlanhq/application-sdk/commit/4736ccd))
- chore(deps): lock file maintenance (#2582) ([d7e773a](https://github.com/atlanhq/application-sdk/commit/d7e773a))
- chore(deps): lock file maintenance (#2581) ([c7dc3e9](https://github.com/atlanhq/application-sdk/commit/c7dc3e9))
- chore(deps): lock file maintenance (#2578) ([c29599b](https://github.com/atlanhq/application-sdk/commit/c29599b))
- chore(deps): lock file maintenance (#2569) ([8ab4b65](https://github.com/atlanhq/application-sdk/commit/8ab4b65))
- chore(deps): lock file maintenance (#2562) ([5ef50d6](https://github.com/atlanhq/application-sdk/commit/5ef50d6))
- chore(deps): lock file maintenance (#2559) ([8b938d1](https://github.com/atlanhq/application-sdk/commit/8b938d1))
- chore(deps): lock file maintenance (#2558) ([e25500d](https://github.com/atlanhq/application-sdk/commit/e25500d))
- chore(deps): lock file maintenance (#2546) ([dd263ff](https://github.com/atlanhq/application-sdk/commit/dd263ff))

## [0.12.0] - 2026-07-07

### Features

- add T005-T015 test-quality rules for coverage meaningfulness (#2540) ([4a8bfb6](https://github.com/atlanhq/application-sdk/commit/4a8bfb6))
- bundle Dapr component YAMLs into the SDK wheel (#2531) ([80bd999](https://github.com/atlanhq/application-sdk/commit/80bd999))

### Bug fixes

- P029 requires extraction_method top-level + exempts non-agent entrypoints (#2536) ([a6b604b](https://github.com/atlanhq/application-sdk/commit/a6b604b))

### Other changes

- chore(deps): lock file maintenance (#2542) ([9da8605](https://github.com/atlanhq/application-sdk/commit/9da8605))
- chore(deps): lock file maintenance (#2539) ([657d744](https://github.com/atlanhq/application-sdk/commit/657d744))
- chore(deps): lock file maintenance (#2535) ([6cf2cc2](https://github.com/atlanhq/application-sdk/commit/6cf2cc2))
- chore(deps): lock file maintenance (#2530) ([5fddad4](https://github.com/atlanhq/application-sdk/commit/5fddad4))
- chore(deps): lock file maintenance (#2528) ([4b7a42d](https://github.com/atlanhq/application-sdk/commit/4b7a42d))

## [0.11.3] - 2026-07-06

### Bug fixes

- exempt tests/integration/ from P017 construction/lifecycle checks (#2515) ([3b5e7ff](https://github.com/atlanhq/application-sdk/commit/3b5e7ff))

### Other changes

- chore(deps): lock file maintenance (#2522) ([5b48681](https://github.com/atlanhq/application-sdk/commit/5b48681))
- chore(deps): lock file maintenance (#2519) ([573bc5b](https://github.com/atlanhq/application-sdk/commit/573bc5b))
- chore(deps): lock file maintenance (#2509) ([d1e2263](https://github.com/atlanhq/application-sdk/commit/d1e2263))

## [0.11.2] - 2026-07-05

### Bug fixes

- mechanically remediate C001/C002 CI findings (#2504) ([f386ed9](https://github.com/atlanhq/application-sdk/commit/f386ed9))
- honor --help/-h on bootstrap without side effects (#2506) ([0fb6287](https://github.com/atlanhq/application-sdk/commit/0fb6287))

### Other changes

- chore(deps): lock file maintenance (#2502) ([edf7ef2](https://github.com/atlanhq/application-sdk/commit/edf7ef2))

## [0.11.1] - 2026-07-04

### Bug fixes

- skip P030 for apps with no publish stage (#2500) ([01e3d6c](https://github.com/atlanhq/application-sdk/commit/01e3d6c))
- suppress T201 inline in build_conformance_args.py (#2486) ([58accfc](https://github.com/atlanhq/application-sdk/commit/58accfc))

### Other changes

- chore(deps): lock file maintenance (#2501) ([b21b1e4](https://github.com/atlanhq/application-sdk/commit/b21b1e4))
- chore(deps): lock file maintenance (#2493) ([4130b08](https://github.com/atlanhq/application-sdk/commit/4130b08))
- chore(deps): lock file maintenance (#2491) ([9756039](https://github.com/atlanhq/application-sdk/commit/9756039))
- chore(deps): lock file maintenance (#2484) ([7c24a41](https://github.com/atlanhq/application-sdk/commit/7c24a41))

## [0.11.0] - 2026-07-03

### Features

- add SourceUnavailableError for customer-controlled source systems (#2476) ([4ba7158](https://github.com/atlanhq/application-sdk/commit/4ba7158))

### Bug fixes

- bootstrap contract ledger scaffold + discovery path bug (#2483) ([3240021](https://github.com/atlanhq/application-sdk/commit/3240021))

### Other changes

- chore(deps): lock file maintenance (#2474) ([afccf21](https://github.com/atlanhq/application-sdk/commit/afccf21))
- chore(deps): lock file maintenance (#2472) ([55b90d5](https://github.com/atlanhq/application-sdk/commit/55b90d5))
- chore(deps): lock file maintenance (#2465) ([82a1f3c](https://github.com/atlanhq/application-sdk/commit/82a1f3c))
- chore(deps): lock file maintenance (#2460) ([1f8f7a6](https://github.com/atlanhq/application-sdk/commit/1f8f7a6))
- chore(deps): lock file maintenance (#2459) ([fa95c66](https://github.com/atlanhq/application-sdk/commit/fa95c66))
- chore(deps): lock file maintenance (#2453) ([4feedd8](https://github.com/atlanhq/application-sdk/commit/4feedd8))

## [0.10.0] - 2026-07-01

### Features

- schedule force-refresh runs of the conformance suite (#2451) ([ec43c58](https://github.com/atlanhq/application-sdk/commit/ec43c58))
- add generated-artifact freshness CI gate (BLDX-1414) (#2440) ([2944741](https://github.com/atlanhq/application-sdk/commit/2944741))
- add S-series secret-hygiene rules S001/S002 (BLDX-1419) (#2436) ([c75dc75](https://github.com/atlanhq/application-sdk/commit/c75dc75))
- add K003/K004/K005 generated-artifact freshness rules (BLDX-1414) (#2439) ([eb6d142](https://github.com/atlanhq/application-sdk/commit/eb6d142))
- T004 DevEntrypointRequiresAppModule (BLDX-1520) (#2448) ([766b4ce](https://github.com/atlanhq/application-sdk/commit/766b4ce))

### Bug fixes

- vendor run-conformance-detect action + arg-builder script into consumer repos (#2445) ([ce42367](https://github.com/atlanhq/application-sdk/commit/ce42367))

### Other changes

- chore(deps): lock file maintenance (#2452) ([d5026de](https://github.com/atlanhq/application-sdk/commit/d5026de))
- chore(deps): lock file maintenance (#2449) ([1f6fa90](https://github.com/atlanhq/application-sdk/commit/1f6fa90))
- ci(conformance): fix age_days test date arithmetic across month boundary (#2443) ([781cb78](https://github.com/atlanhq/application-sdk/commit/781cb78))

## [0.9.0] - 2026-06-30

### Features

- add --enforce flag for soft-rollout mode (#2426) ([daec2ea](https://github.com/atlanhq/application-sdk/commit/daec2ea))
- add SDR-readiness rules P029, P030, T002, T003 (#2425) ([1a7917d](https://github.com/atlanhq/application-sdk/commit/1a7917d))
- add E020 HttpFailureToEmptyReturn (BLDX-1503) (#2418) ([356e86b](https://github.com/atlanhq/application-sdk/commit/356e86b))
- add P-series rules P026/P027/P028 (BLDX-1499/1500/1501) (#2417) ([ab88dbe](https://github.com/atlanhq/application-sdk/commit/ab88dbe))
- add E019 ExceptionTextInContractField (BLDX-1498) (#2405) ([978137d](https://github.com/atlanhq/application-sdk/commit/978137d))
- add K-series contract-toolkit conformance rules (BLDX-1479) (#2424) ([1ce6be1](https://github.com/atlanhq/application-sdk/commit/1ce6be1))
- enforce correct asset-mapper usage (pyatlan_v9, serialization, typed returns) (#2385) ([11a4f2b](https://github.com/atlanhq/application-sdk/commit/11a4f2b))
- app-name alignment gate P025 (#2386) ([564d260](https://github.com/atlanhq/application-sdk/commit/564d260))
- flag legacy transformer usage via B001, steer apps to asset-mapper (BLDX-1399) (#2382) ([49b9fa4](https://github.com/atlanhq/application-sdk/commit/49b9fa4))
- async/determinism workflow checks P020-P024 (#2377) ([3717f24](https://github.com/atlanhq/application-sdk/commit/3717f24))
- client-seam gate P019 — no raw HTTP to Atlan (BLDX-1430) (#2375) ([7a23c92](https://github.com/atlanhq/application-sdk/commit/7a23c92))

### Bug fixes

- add pull-requests: read for private repos; fix gen-contract-ledger outfile default (#2414) ([fec743c](https://github.com/atlanhq/application-sdk/commit/fec743c))

### Other changes

- chore(deps): lock file maintenance (#2435) ([9c3bc82](https://github.com/atlanhq/application-sdk/commit/9c3bc82))
- chore(deps): lock file maintenance (#2434) ([84c688f](https://github.com/atlanhq/application-sdk/commit/84c688f))
- chore(deps): lock file maintenance (#2422) ([38b4d33](https://github.com/atlanhq/application-sdk/commit/38b4d33))
- chore(deps): lock file maintenance (#2421) ([ecd98a6](https://github.com/atlanhq/application-sdk/commit/ecd98a6))
- chore(deps): lock file maintenance (#2420) ([f6f5949](https://github.com/atlanhq/application-sdk/commit/f6f5949))
- chore(deps): lock file maintenance (#2413) ([1c45527](https://github.com/atlanhq/application-sdk/commit/1c45527))
- chore(deps): lock file maintenance (#2410) ([24df019](https://github.com/atlanhq/application-sdk/commit/24df019))
- chore(deps): lock file maintenance (#2402) ([1ec4f4a](https://github.com/atlanhq/application-sdk/commit/1ec4f4a))
- chore(deps): lock file maintenance (#2388) ([f5fd836](https://github.com/atlanhq/application-sdk/commit/f5fd836))
- chore(deps): lock file maintenance (#2384) ([f4c14a9](https://github.com/atlanhq/application-sdk/commit/f4c14a9))
- chore(deps): lock file maintenance (#2376) ([8062a7f](https://github.com/atlanhq/application-sdk/commit/8062a7f))
- chore(deps): lock file maintenance (#2371) ([711fc7a](https://github.com/atlanhq/application-sdk/commit/711fc7a))

## [0.8.0] - 2026-06-25

### Features

- P017/P018 entrypoint-conformance rules (BLDX-1411) (#2355) ([8342bc6](https://github.com/atlanhq/application-sdk/commit/8342bc6))
- additive-only contract gate B005/B006 (BLDX-1425) (#2350) ([225c8ed](https://github.com/atlanhq/application-sdk/commit/225c8ed))

### Bug fixes

- ignore action SHA pins in C002 drift comparison (#2358) ([1224379](https://github.com/atlanhq/application-sdk/commit/1224379))
- make %-style log args lazily evaluated (#2353) ([4fcc98b](https://github.com/atlanhq/application-sdk/commit/4fcc98b))

### Other changes

- chore(deps): lock file maintenance (#2363) ([4f8b0a1](https://github.com/atlanhq/application-sdk/commit/4f8b0a1))
- chore(deps): lock file maintenance (#2360) ([fdf1e27](https://github.com/atlanhq/application-sdk/commit/fdf1e27))
- chore(deps): lock file maintenance (#2342) ([6a2e7f3](https://github.com/atlanhq/application-sdk/commit/6a2e7f3))
- chore(deps): lock file maintenance (#2340) ([e1b98d1](https://github.com/atlanhq/application-sdk/commit/e1b98d1))

## [0.7.0] - 2026-06-25

### Features

- add P016 EntryPointContractCodeDrift rule (BLDX-1425) (#2339) ([a5c8f2a](https://github.com/atlanhq/application-sdk/commit/a5c8f2a))
- remove daft entirely, replace with pyarrow/orjson/duckdb (#2300) ([41f32e1](https://github.com/atlanhq/application-sdk/commit/41f32e1))
- P013/P014/P015 typed-contract boundary rules (BLDX-1413) (#2336) ([300efb7](https://github.com/atlanhq/application-sdk/commit/300efb7))
- Renovate fleet dashboard data feed (BLDX-1468) (#2289) ([9d3826c](https://github.com/atlanhq/application-sdk/commit/9d3826c))

### Bug fixes

- tolerate regenerate-contract override in C002 drift (#2323) ([55802cd](https://github.com/atlanhq/application-sdk/commit/55802cd))
- resolve ARG-based FROM base image in I001 check (#2324) ([6118510](https://github.com/atlanhq/application-sdk/commit/6118510))
- update conformance dashboard on every push to main (#2292) ([e6e32ef](https://github.com/atlanhq/application-sdk/commit/e6e32ef))

### Other changes

- chore(deps): lock file maintenance (#2337) ([c41c57a](https://github.com/atlanhq/application-sdk/commit/c41c57a))
- chore(deps): lock file maintenance (#2332) ([94de56b](https://github.com/atlanhq/application-sdk/commit/94de56b))
- chore(deps): lock file maintenance (#2296) ([fabafa1](https://github.com/atlanhq/application-sdk/commit/fabafa1))
- chore(deps): lock file maintenance (#2291) ([d767fa7](https://github.com/atlanhq/application-sdk/commit/d767fa7))
- chore(deps): lock file maintenance (#2284) ([90825e1](https://github.com/atlanhq/application-sdk/commit/90825e1))

## [0.6.0] - 2026-06-20

### Features

- add I-series Dockerfile conformance rules (I001–I005) (#2256) ([aff34a3](https://github.com/atlanhq/application-sdk/commit/aff34a3))
- orchestration-seam rules P004-P007 (BLDX-1417) (#2255) ([392922d](https://github.com/atlanhq/application-sdk/commit/392922d))
- add D003 rule to warn about unused dependencies (BLDX-1462) (#2253) ([138ffc2](https://github.com/atlanhq/application-sdk/commit/138ffc2))
- add T001 rule for integration test marking (#2224) ([be1af6e](https://github.com/atlanhq/application-sdk/commit/be1af6e))

### Bug fixes

- correct detect and test commands in remediate prose (#2246) ([9808adc](https://github.com/atlanhq/application-sdk/commit/9808adc))

### Other changes

- chore(deps): lock file maintenance (#2262) ([0534b42](https://github.com/atlanhq/application-sdk/commit/0534b42))
- chore(deps): lock file maintenance (#2259) ([4be04ef](https://github.com/atlanhq/application-sdk/commit/4be04ef))
- chore(deps): lock file maintenance (#2251) ([e6d04c2](https://github.com/atlanhq/application-sdk/commit/e6d04c2))
- chore(deps): lock file maintenance (#2243) ([79eed5d](https://github.com/atlanhq/application-sdk/commit/79eed5d))
- chore(deps): lock file maintenance (#2236) ([68e1083](https://github.com/atlanhq/application-sdk/commit/68e1083))

## [0.5.0] - 2026-06-19

### Features

- L-series AST checker for logging anti-patterns (BLDX-1437) (#2221) ([c887557](https://github.com/atlanhq/application-sdk/commit/c887557))
- rule scope (sdk/app/both) + wire D-series CI (#2216) ([85d7296](https://github.com/atlanhq/application-sdk/commit/85d7296))
- add C003 GitignoreMissingEntry check (BLDX-1452) (#2209) ([44f8344](https://github.com/atlanhq/application-sdk/commit/44f8344))

### Bug fixes

- decouple SARIF upload from gate to fix Security tab errors (#2219) ([a7b0ac3](https://github.com/atlanhq/application-sdk/commit/a7b0ac3))
- use conformance suite version in rule since fields (#2210) ([737e67b](https://github.com/atlanhq/application-sdk/commit/737e67b))
- use ORG_PAT_GITHUB for bot-pushes on renovate branches (#2200) ([d7418bf](https://github.com/atlanhq/application-sdk/commit/d7418bf))

### Other changes

- chore(deps): lock file maintenance (#2228) ([1bd8864](https://github.com/atlanhq/application-sdk/commit/1bd8864))
- chore(deps): lock file maintenance (#2215) ([c1ea14b](https://github.com/atlanhq/application-sdk/commit/c1ea14b))
- chore(deps): lock file maintenance (#2199) ([1a2cdac](https://github.com/atlanhq/application-sdk/commit/1a2cdac))

## [0.4.0] - 2026-06-18

### Features

- rule rationale field + L-series surgery + P/O catalog wiring (#2191) ([08be5ab](https://github.com/atlanhq/application-sdk/commit/08be5ab))
- add P003 ErrorCodePrefixMismatch (BLDX-1431) (#2175) ([bd67c52](https://github.com/atlanhq/application-sdk/commit/bd67c52))
- D001/D002 pyproject.toml conformance against the SDK contract (BLDX-1410) (#2182) ([c605d4a](https://github.com/atlanhq/application-sdk/commit/c605d4a))
- P002 CategoryFieldOverride — enforce immutable FailureCategory taxonomy (BLDX-1432) (#2174) ([b557098](https://github.com/atlanhq/application-sdk/commit/b557098))
- smarter bootstrap with auto-detection and renovate.json scaffold (#2184) ([dc864d5](https://github.com/atlanhq/application-sdk/commit/dc864d5))
- add P-series prescriptions and O-series optimizations (P001 unbounded contracts, O001 stdlib json) (#2162) ([8850269](https://github.com/atlanhq/application-sdk/commit/8850269))

## [0.3.0] - 2026-06-16

### Features

- clean up GitHub check names across tests-reusable + conformance-reusable (#2172) ([cf6b362](https://github.com/atlanhq/application-sdk/commit/cf6b362))
- tests-reusable.yaml + services-script hook + tests.yaml scaffold (#2170) ([72f070b](https://github.com/atlanhq/application-sdk/commit/72f070b))
- bake 16 standard CI shims + C002 drift check (#2155) ([5b01f76](https://github.com/atlanhq/application-sdk/commit/5b01f76))

## [0.2.0] - 2026-06-15

### Features

- extract conformance suite to standalone publishable package (#2138) ([595d0e8](https://github.com/atlanhq/application-sdk/commit/595d0e8))

