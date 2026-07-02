# Changelog

All notable changes to `atlan-application-sdk-conformance` are documented here.

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

