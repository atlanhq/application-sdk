# Changelog


## v0.1.1rc21 (July 10, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc20...v0.1.1rc21

### Features

- enable tests and examples on macOS and windows as part of CI (#606) (by @Nishchith Shetty in [e717fa0](https://github.com/atlanhq/application-sdk/commit/e717fa0))

### Bug Fixes

- multi-database fetch based on include / exclude filter (#610) (by @Abhishek Agrawal in [954584e](https://github.com/atlanhq/application-sdk/commit/954584e))
- unit tests structure, add missing tests (#609) (by @Nishchith Shetty in [b146004](https://github.com/atlanhq/application-sdk/commit/b146004))

## v0.1.1rc20 (July 02, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc19...v0.1.1rc20

### Bug Fixes

- uvloop not supported for windows, docs (#604) (by @Nishchith Shetty in [bd07e9f](https://github.com/atlanhq/application-sdk/commit/bd07e9f))



## v0.1.1rc19 (June 26, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc18...v0.1.1rc19

### Features

- Add application-sdk support to write markers during query extraction (#599) (by @Abhishek Agrawal in [1d97e81](https://github.com/atlanhq/application-sdk/commit/1d97e81))

### Bug Fixes

- defaults for fetch_metadata endpoint, simplify handler (#598) (by @Nishchith Shetty in [1c7f0ff](https://github.com/atlanhq/application-sdk/commit/1c7f0ff))
- switch to debug level for logs outside workflow/activity context (#594) (by @SanilK2108 in [2a56df4](https://github.com/atlanhq/application-sdk/commit/2a56df4))

## v0.1.1rc18 (June 19, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc17...v0.1.1rc18

### Bug Fixes

- setup_workflow method for metadata_extraction (#583) (by @Abhishek Agrawal in [1e00f7e](https://github.com/atlanhq/application-sdk/commit/1e00f7e))
    - ⚠️ Note : This is a breaking change. Please update your workflows to pass the workflow and activities classes as a tuple.


## v0.1.1rc17 (June 18, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc16...v0.1.1rc17

### Features

- observability improvements (#584) (by @SanilK2108 in [cc11cb9](https://github.com/atlanhq/application-sdk/commit/cc11cb9))

## v0.1.1rc16 (June 17, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc15...v0.1.1rc16

### Features

- Improve attribute definitions in yaml templates for SQL transformer (#528) (by @Onkar Ravgan in [be2cbd0](https://github.com/atlanhq/application-sdk/commit/be2cbd0))

### Bug Fixes

- Fetch queries activity in SQLQueryExtractionActivities (#587) (by @Abhishek Agrawal in [708f783](https://github.com/atlanhq/application-sdk/commit/708f783))



## v0.1.1rc15 (June 16, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc14...v0.1.1rc15

### Features

- add support for event based workflows (#560) (by @SanilK2108 in [27d8a13](https://github.com/atlanhq/application-sdk/commit/27d8a13))

### Bug Fixes

- workflow argument handling in test classes (#582) (by @Mustafa in [01ab925](https://github.com/atlanhq/application-sdk/commit/01ab925))

## v0.1.1rc14 (June 10, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc13...v0.1.1rc14

### Bug Fixes

- dapr limit while file upload (#576) (by @Onkar Ravgan in [32f63d6](https://github.com/atlanhq/application-sdk/commit/32f63d6))



## v0.1.1rc13 (June 10, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc12...v0.1.1rc13

### Bug Fixes

- update compiled_url_logic (#574) (by @Onkar Ravgan in [c1c6253](https://github.com/atlanhq/application-sdk/commit/c1c6253))

## v0.1.1rc12 (June 05, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc11...v0.1.1rc12

### Bug Fixes

- Pandas read_sql for Redshift adbc connection (#572) (by @Onkar Ravgan in [183d204](https://github.com/atlanhq/application-sdk/commit/183d204))



## v0.1.1rc11 (June 02, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc10...v0.1.1rc11

### Bug Fixes

- changed application -> server, custom servers in constructor in [16a3f7f](https://github.com/atlanhq/application-sdk/commit/16a3f7f81b7a26400019c76611ec6ee327ea9e1a)


## v0.1.1rc10 (May 29, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc9...v0.1.1rc10

### Features

- add observability decorator (#559) (by @Abhishek Agrawal in [1dd4c82](https://github.com/atlanhq/application-sdk/commit/1dd4c82))



## v0.1.1rc9 (May 28, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc8...v0.1.1rc9

### Features

- add support for sync activity executor (#563) (by @Nishchith Shetty in [fe5f396](https://github.com/atlanhq/application-sdk/commit/fe5f396))

## v0.1.1rc8 (May 28, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc7...v0.1.1rc8

### Features

- Add SQLAlchemy url support (#561) (by @Onkar Ravgan in [67bc050](https://github.com/atlanhq/application-sdk/commit/67bc050))



## v0.1.1rc7 (May 28, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc6...v0.1.1rc7

### Bug Fixes

- issue with retrieving workflow args (#562) (by @Nishchith Shetty in [1f3f194](https://github.com/atlanhq/application-sdk/commit/1f3f194))
- Observability (duckDB UI) (#556) (by @Abhishek Agrawal in [d06b52a](https://github.com/atlanhq/application-sdk/commit/d06b52a))

## v0.1.1rc6 (May 20, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc5...v0.1.1rc6

### Features

- Observability changes (metrics, logs, traces) by @abhishekagrawal-atlan + refactoring
- Enhancements and standardizing of Error Codes by @abhishekagrawal-atlan
- Transition to Pandas for SQL Querying on Source and Daft for SQL Transformer by @OnkarVO7
- fix: JsonOutput type checking while writing dataframe by @Hk669
- feat: enhance workflow activity collection and allow custom output @TechyMT
- Dependabot changes - version bump
- improvements to documentation and debugging (#547) (by @inishchith in [49d51f2](https://github.com/atlanhq/application-sdk/commit/49d51f2))
- update readme (by @AtMrun in [28b74ea](https://github.com/atlanhq/application-sdk/commit/28b74ea))
- add common setup issues (#542) (by @inishchith in [dd18a31](https://github.com/atlanhq/application-sdk/commit/dd18a31))



## v0.1.1rc5 (May 13, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc4...v0.1.1rc5

### Bug Fixes

- uv optional dependencies and groups (by @inishchith in [b789965](https://github.com/atlanhq/application-sdk/commit/b789965))

## v0.1.1rc4 (May 13, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc3...v0.1.1rc4

### Features

- migrate to uv, goodbye poetry (#485) (by @Nishchith Shetty in [6f0570f](https://github.com/atlanhq/application-sdk/commit/6f0570f))



## v0.1.1rc3 (May 13, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc2...v0.1.1rc3

### Features

- Add SQL based transformer mapper (#423) (by @Onkar Ravgan in [459e806](https://github.com/atlanhq/application-sdk/commit/459e806))

### Bug Fixes

- dependabot issues (by @inishchith in [089d4f8](https://github.com/atlanhq/application-sdk/commit/089d4f8))
- sql transformer diff (#511) (by @Onkar Ravgan in [5641a7a](https://github.com/atlanhq/application-sdk/commit/5641a7a))
- date datatype in transformed output (by @Onkar Ravgan in [032bbbd](https://github.com/atlanhq/application-sdk/commit/032bbbd))

## v0.1.1rc2 (May 12, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc1...v0.1.1rc2



## v0.1.1rc1 (May 08, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc0...v0.1.1rc1

### Features

- Improve release flow - Github and PyPi (by @inishchith in [e96c2b7](https://github.com/atlanhq/application-sdk/commit/e96c2b7))
- fix pipeline (#468) (by @Junaid Rahim in [f6fc5b7](https://github.com/atlanhq/application-sdk/commit/f6fc5b7))
- basic docs enhancements (#460) (by @Nishchith Shetty in [1983e79](https://github.com/atlanhq/application-sdk/commit/1983e79))

### Bug Fixes

- remove redundant dependency (pylint) (by @Nishchith Shetty in [55d0169](https://github.com/atlanhq/application-sdk/commit/55d0169))

## v0.1.0-rc.1 (May 06, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.0...v0.1.0-rc.1

### Features

- Add passthrough_modules parameter to setup_workflow method by @TechyMT

### Bug Fixes

- CodeQL advanced analysis errors
- Add note around copyleft license compliance in pull request templates
- Tests for recent refactor

### Chores

- Bump poetry to 2.1.3
- Disable krytonite docs upload steps (will be resumed)

### Notes

- We plan evolve our release tagging schemes over the next few days (ideally, a release candidate must be prior to a release)


## v0.1.0 (May 05, 2025)

- Initial public release :tada:

