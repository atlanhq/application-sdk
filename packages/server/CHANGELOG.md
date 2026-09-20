# Changelog

All notable changes to `atlan-application-sdk-server` are documented here.

## 0.1.0

### Added

- Initial release. The consolidated API server runtime — auth / preflight /
  metadata handler surface, SQLAlchemy client, pluggable config store and
  FastAPI assembly — moved here from the standalone `atlanhq/server-sdk`
  repository so it ships on the Application SDK's release train, conformance
  gate and Renovate auto-propagation (ARUN-942).
