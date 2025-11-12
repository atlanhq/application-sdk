# Changelog

## v0.1.1rc63 (November 12, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc62...v0.1.1rc63

### Features

- handle single key secret stores (#812) (by @nishantmunjal7 in [abf5f6d](https://github.com/atlanhq/application-sdk/commit/abf5f6d))
- deployment secret single-key (#816) (by @nishantmunjal7 in [c5efd9a](https://github.com/atlanhq/application-sdk/commit/c5efd9a))


## v0.1.1rc62 (November 03, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc61...v0.1.1rc62

### Features

- Adding a new event whenever Token refreshes (#802) (by @Garavitey in [2e32ab4](https://github.com/atlanhq/application-sdk/commit/2e32ab4))


## v0.1.1rc61 (October 30, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc60...v0.1.1rc61


## v0.1.1rc60 (October 29, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc59...v0.1.1rc60

### Features

- Fixing the daft import issue (#803) (by @Garavitey in [0f27353](https://github.com/atlanhq/application-sdk/commit/0f27353))

### Bug Fixes

- bump Dapr cli to v1.16.2, helm security patch (#804) (by @inishchith in [d36a4e7](https://github.com/atlanhq/application-sdk/commit/d36a4e7))


## v0.1.1rc59 (October 25, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc58...v0.1.1rc59

### Features

- Adding support to push logs decoupled for workflow (#786) (by @Garavitey in [2b291ff](https://github.com/atlanhq/application-sdk/commit/2b291ff))


## v0.1.1rc58 (October 22, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc57...v0.1.1rc58

### Bug Fixes

- Updated lockname to not use APPLICATION_NAME (#797) (by @SanilK2108 in [151b811](https://github.com/atlanhq/application-sdk/commit/151b811))


## v0.1.1rc57 (October 22, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc56...v0.1.1rc57

### Bug Fixes

- remove long running local activities (#792) (by @inishchith in [e046c90](https://github.com/atlanhq/application-sdk/commit/e046c90))


## v0.1.1rc56 (October 15, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc55...v0.1.1rc56

### Features

- update readme file (#787) (by @junaidrahim in [2ccc631](https://github.com/atlanhq/application-sdk/commit/2ccc631))

### Bug Fixes

- Not logging each metric (#790) (by @TechyMT in [42a8f0f](https://github.com/atlanhq/application-sdk/commit/42a8f0f))


## v0.1.1rc55 (October 14, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc54...v0.1.1rc55

### Features

- add connect_args support to DatabaseConfig (#784) (by @inishchith in [f8ad608](https://github.com/atlanhq/application-sdk/commit/f8ad608))


## v0.1.1rc54 (October 09, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc53...v0.1.1rc54

### Bug Fixes

- Add file converter to sdk for QI app Json Support (#778) (by @OnkarVO7 in [177aa06](https://github.com/atlanhq/application-sdk/commit/177aa06))


## v0.1.1rc52 (October 03, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc51...v0.1.1rc52

### Bug Fixes

- incorrect temporal workflow run-id reported (#750) (by @inishchith in [9e8ce3e](https://github.com/atlanhq/application-sdk/commit/9e8ce3e))
- resolve race condition in preflight_check activity (#751) (by @inishchith in [d24fe41](https://github.com/atlanhq/application-sdk/commit/d24fe41))


## v0.1.1rc51 (October 01, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc50...v0.1.1rc51

### Features

- enable Dapr hot reload and add error resilience to components (#744) (by @inishchith in [064f944](https://github.com/atlanhq/application-sdk/commit/064f944))

### Bug Fixes

- Adjust max file size buffer in ParquetOutput to 75% of DAPR limit (#741) (by @TechyMT in [727abef](https://github.com/atlanhq/application-sdk/commit/727abef))


## v0.1.1rc50 (September 29, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc49...v0.1.1rc50

### Features

- bump Dapr version to 1.16.0 (#729) (by @inishchith in [02dab1c](https://github.com/atlanhq/application-sdk/commit/02dab1c))


## v0.1.1rc49 (September 29, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc48...v0.1.1rc49

### Bug Fixes

- Make Changelog script use GitHub usernames instead of Git Author Names (#735) (by @drockparashar in [244b53f](https://github.com/atlanhq/application-sdk/commit/244b53f))
- add env token to generate changelog step (#739) (by @inishchith in [8278e58](https://github.com/atlanhq/application-sdk/commit/8278e58))


## v0.1.1rc48 (September 26, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc47...v0.1.1rc48

### Bug Fixes

- Updated constants + Refactor for multidb logic (#736) (by @Abhishek Agrawal in [cd97be3](https://github.com/atlanhq/application-sdk/commit/cd97be3))

## v0.1.1rc47 (September 24, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc46...v0.1.1rc47

### Bug Fixes

- partition columns defaults for daft write_parquet (#728) (by @Nishchith Shetty in [7ed4949](https://github.com/atlanhq/application-sdk/commit/7ed4949))
- Update ParquetOutput to handle file uploads correctly (#732) (by @Mustafa in [b4b2953](https://github.com/atlanhq/application-sdk/commit/b4b2953))
- CI build path (#733) (by @Nishchith Shetty in [6d04006](https://github.com/atlanhq/application-sdk/commit/6d04006))



## v0.1.1rc46 (September 18, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc45...v0.1.1rc46

### Bug Fixes

- Fix semaphore implementation to allow multiple batching without getting into max_concurrent_activities issue (#725) (by @Mustafa in [2a66f13](https://github.com/atlanhq/application-sdk/commit/2a66f13))

## v0.1.1rc45 (September 18, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc44...v0.1.1rc45

### Features

- add MCP (Model Context Protocol) integration to application-sdk (#698) (by @AdvitXAtlan in [a5d496e](https://github.com/atlanhq/application-sdk/commit/a5d496e))
- fix credentials and extra field information in docs (#724) (by @Nishchith Shetty in [a0808db](https://github.com/atlanhq/application-sdk/commit/a0808db))
- add minimal API docs for endpoints exposed (#721) (by @Nishchith Shetty in [3301b61](https://github.com/atlanhq/application-sdk/commit/3301b61))



## v0.1.1rc44 (September 17, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc43...v0.1.1rc44

### Bug Fixes

- memory management issues with readers and writers (#700) (by @Nishchith Shetty in [af77ed9](https://github.com/atlanhq/application-sdk/commit/af77ed9))

## v0.1.1rc43 (September 16, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc42...v0.1.1rc43

### Features

- update configuration documentation with comprehensive constant descriptions and use cases (#646) (by @Nishchith Shetty in [6f91796](https://github.com/atlanhq/application-sdk/commit/6f91796))

### Bug Fixes

- file not found exception across cloud providers, suppress logs in case of upsert (#702) (by @Nishchith Shetty in [0089c5f](https://github.com/atlanhq/application-sdk/commit/0089c5f))



## v0.1.1rc42 (September 15, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc41...v0.1.1rc42

### Features

- enhance file handling in Input classes (#697) (by @Mustafa in [a792fa6](https://github.com/atlanhq/application-sdk/commit/a792fa6))

## v0.1.1rc41 (September 11, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc40...v0.1.1rc41

### Features

- add support for multidb extraction, enhance aws client via redshift refactor (#706) (by @Abhishek Agrawal in [4441285](https://github.com/atlanhq/application-sdk/commit/4441285))
- add support for frontend configmap handler and static file serving (#631) (by @Angad Sethi in [13e164a](https://github.com/atlanhq/application-sdk/commit/13e164a))



## v0.1.1rc40 (September 09, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc39...v0.1.1rc40

### Features

- add retain_local_copy option to upload methods in ObjectStore (#691) (by @Mustafa in [38285f0](https://github.com/atlanhq/application-sdk/commit/38285f0))
- enhance bugbot rules (#681) (by @AdvitXAtlan in [546d23f](https://github.com/atlanhq/application-sdk/commit/546d23f))

### Bug Fixes

- make sql client use context managers (#699) (by @Nishchith Shetty in [211f31b](https://github.com/atlanhq/application-sdk/commit/211f31b))

## v0.1.1rc39 (September 04, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc38...v0.1.1rc39

### Features

- implement delete file and prefix operations in ObjectStore (#694) (by @Mustafa in [6f71615](https://github.com/atlanhq/application-sdk/commit/6f71615))
- add Daft configuration constants and update Parquet output handling (by @Mustafa in [68c2593](https://github.com/atlanhq/application-sdk/commit/68c2593))



## v0.1.1rc38 (September 03, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc37...v0.1.1rc38

## v0.1.1rc37 (September 03, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc36...v0.1.1rc37

### Features

- add file size-based chunking to JsonOutput (#650) (by @Nishchith Shetty in [7e7685b](https://github.com/atlanhq/application-sdk/commit/7e7685b))



## v0.1.1rc36 (September 01, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc35...v0.1.1rc36

### Features

- Update to pyatlan 8.0.2 to get fix for AsyncWorkflowSearchResponse async iterator (#680) (by @Ernest Hill in [a33bc4c](https://github.com/atlanhq/application-sdk/commit/a33bc4c))
- implement Redis client with sync/async support and distributed locking (#621) (by @Mustafa in [e384ceb](https://github.com/atlanhq/application-sdk/commit/e384ceb))
- implement abstract methods in BaseHandler (by @Mustafa in [c7c1937](https://github.com/atlanhq/application-sdk/commit/c7c1937))

## v0.1.1rc35 (August 28, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc34...v0.1.1rc35

### Features

- improve services abstraction (#658) (by @Nishchith Shetty in [f5a4c4e](https://github.com/atlanhq/application-sdk/commit/f5a4c4e))



## v0.1.1rc34 (August 27, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc33...v0.1.1rc34

### Features

- Generic Client and Handlers for non-SQL connectors with http request methods (allows custom retry transport) (#660) (by @Prateek Rai in [b077a90](https://github.com/atlanhq/application-sdk/commit/b077a90))

## v0.1.1rc33 (August 26, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc32...v0.1.1rc33



## v0.1.1rc32 (August 20, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc31...v0.1.1rc32

### Bug Fixes

- update app host (#664) (by @Onkar Ravgan in [24d23cb](https://github.com/atlanhq/application-sdk/commit/24d23cb))
- file references in cursor rules (#662) (by @Nishchith Shetty in [2395ce3](https://github.com/atlanhq/application-sdk/commit/2395ce3))
- Parquet file output path with None file names (#659) (by @Onkar Ravgan in [8841660](https://github.com/atlanhq/application-sdk/commit/8841660))

## v0.1.1rc31 (August 14, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc30...v0.1.1rc31

### Features

- support for app registration via events (#635) (by @Nishant Munjal in [330b585](https://github.com/atlanhq/application-sdk/commit/330b585))

### Bug Fixes

- error logs related to deployment secrets (#651) (by @Nishchith Shetty in [09f6e5c](https://github.com/atlanhq/application-sdk/commit/09f6e5c))



## v0.1.1rc30 (August 13, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc29...v0.1.1rc30

### Bug Fixes

- download_files_from_object_store with correct files list (#652) (by @Nishchith Shetty in [e2ed7bd](https://github.com/atlanhq/application-sdk/commit/e2ed7bd))

## v0.1.1rc29 (August 13, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc28...v0.1.1rc29

### Features

- increase DAPR gRPC message limit from 16MB to 100MB (#649) (by @Nishchith Shetty in [ae0d218](https://github.com/atlanhq/application-sdk/commit/ae0d218))

### Bug Fixes

- error logs for eventstore, objectstore and metrics (#647) (by @Nishchith Shetty in [70b71b6](https://github.com/atlanhq/application-sdk/commit/70b71b6))



## v0.1.1rc28 (August 12, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc27...v0.1.1rc28

### Features

- add pyatlan Client to Application SDK (#642) (by @Ernest Hill in [c00d682](https://github.com/atlanhq/application-sdk/commit/c00d682))

## v0.1.1rc27 (August 12, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc26...v0.1.1rc27

### Bug Fixes

- remove ATLAN_LOCAL_DEVELOPMENT env, infer usage based on components (#641) (by @Nishchith Shetty in [58462e7](https://github.com/atlanhq/application-sdk/commit/58462e7))
- replace getdaft with daft (#639) (by @Nishchith Shetty in [de79e93](https://github.com/atlanhq/application-sdk/commit/de79e93))
- cleanup local path post upload to object store (#640) (by @Nishchith Shetty in [4195425](https://github.com/atlanhq/application-sdk/commit/4195425))



## v0.1.1rc26 (August 07, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc25...v0.1.1rc26

### Features

- add cursor and bugbot rules (#636) (by @Amit Prabhu in [cbbecd9](https://github.com/atlanhq/application-sdk/commit/cbbecd9))

### Bug Fixes

- handle str and dict types in preflight checks (#638) (by @Nishchith Shetty in [57ee9a0](https://github.com/atlanhq/application-sdk/commit/57ee9a0))

## v0.1.1rc25 (August 05, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc24...v0.1.1rc25

### Features

- implement OAuth2 authentication and data transfer for applications (#634) (by @nishantmunjal7 in [e5e70a2](https://github.com/atlanhq/application-sdk/commit/e5e70a2))



## v0.1.1rc24 (August 01, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc23...v0.1.1rc24

### Features

- Add utility function to fetch include/exclude databases from include-exclude regex (#623) (by @Abhishek Agrawal in [0c6cc62](https://github.com/atlanhq/application-sdk/commit/0c6cc62))

### Bug Fixes

- miner output path (#626) (by @Onkar Ravgan in [cabe7eb](https://github.com/atlanhq/application-sdk/commit/cabe7eb))
- db name regex pattern (#630) (by @Onkar Ravgan in [bb26992](https://github.com/atlanhq/application-sdk/commit/bb26992))

## v0.1.1rc23 (July 29, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc22...v0.1.1rc23

### Bug Fixes

- marker file being saved at incorrect location (#620) (by @Abhishek Agrawal in [d3de56f](https://github.com/atlanhq/application-sdk/commit/d3de56f))
- issues with inferring workflow_run_id (#624) (by @Nishchith Shetty in [61e7a4d](https://github.com/atlanhq/application-sdk/commit/61e7a4d))



## v0.1.1rc22 (July 21, 2025)

Full Changelog: https://github.com/atlanhq/application-sdk/compare/v0.1.1rc21...v0.1.1rc22

### Features

- use object store as state store, path update and fixes (#618) (by @Nishchith Shetty in [d439cb4](https://github.com/atlanhq/application-sdk/commit/d439cb4))

### Bug Fixes

- suppress daft dependency loggers (#611) (by @Nishchith Shetty in [3e379d4](https://github.com/atlanhq/application-sdk/commit/3e379d4))


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

