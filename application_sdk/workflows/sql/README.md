# SQL Workflows

This module enables developers to build SQL workflows that can be executed on the Atlan Platform.

## Table of contents


## Authentication
This interface is used to authenticate the SQL workflow. For example, if the SQL workflow is used to connect to a database, the `test_auth` method is used to test the authentication credentials.

## Metadata
This interface is used to fetch metadata from the SQL source. For example, if the SQL workflow is used to connect to a database, the `fetch_metadata` method is used to fetch the metadata of the database.


## Preflight Check
This interface is used to perform preflight checks before executing the SQL workflow. For example, if the SQL workflow is used to connect to a database, the `preflight_check` method is used to check if the database is reachable.


## Worker
This class provides a default implementation for the SQL workflow, with hooks for subclasses to customize specific behaviors.
