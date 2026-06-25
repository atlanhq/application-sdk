# Changelog

All notable changes to `atlan-application-sdk-conformance` are documented here.

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

