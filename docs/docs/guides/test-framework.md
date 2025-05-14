# Test Framework in Application SDK

The application SDK provides a test framework to help you test your application.

- Examples of test files can be found in the [atlan-postgres-app](https://github.com/atlanhq/atlan-postgres-app/tree/main/tests/e2e/test_postgres_workflow) directory.


## BaseTest Class

The `BaseTest` class provides a base implementation for end-to-end testing of workflows. It includes several key test methods that run in a specific order:

| Test Method            | Order | Description                                                                                                                                                                                                                                       |
| ---------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_health_check`    | 1     | Verifies that the server is running and responding to requests by making a GET request to the host.                                                                                                                                               |
| `test_auth`            | 2     | Tests the authentication and connection flow by calling `test_connection` with provided credentials and validating against expected responses.                                                                                                    |
| `test_metadata`        | 3     | Tests metadata retrieval by calling `get_metadata` with credentials and comparing to expected responses.                                                                                                                                          |
| `test_preflight_check` | 4     | Executes preflight checks using credentials and metadata, validating against expected responses.                                                                                                                                                  |
| `test_run_workflow`    | 5     | Tests the full workflow execution: <br> - Starts the workflow with credentials, metadata and connection details <br> - Monitors the workflow until completion <br> - Raises a `WorkflowExecutionError` if workflow does not complete successfully |
| `test_data_validation` | 6     | Validates the extracted source data                                                                                                                                                                                                               |

The class requires several properties to be defined:

- `config_file_path`: Path to configuration file
- `extracted_output_base_path`: Base path for extracted output
- `schema_base_path`: Base path for schema files
- `credentials`: Dictionary containing authentication credentials
- `metadata`: Dictionary containing metadata
- `connection`: Dictionary containing connection details

It inherits from `TestInterface` and uses pytest's ordering to ensure tests run in the correct sequence.

## Configuration

The `BaseTest` class requires a configuration file to be provided. The configuration file is used to configure the test framework and the workflow.

The configuration file is a YAML file that contains the following:

You can find an example of the configuration file in the [atlan-postgres-app](https://github.com/atlanhq/atlan-postgres-app/blob/main/tests/e2e/test_postgres_workflow/config.yaml).

## Data Validation

The `BaseTest` class provides a `test_data_validation` method that can be used to validate the extracted data.

This uses the [pandera](https://pandera.readthedocs.io/en/stable/) library to validate the extracted data against the schema.

You can find an example of the schema files in the [atlan-postgres-app](https://github.com/atlanhq/atlan-postgres-app/tree/main/tests/e2e/test_postgres_workflow/schema).

The  `test_data_validation` method can be overridden to add custom validation logic.

## Running Tests

You can run the tests normally using pytest:

The following commands are for running the tests using the test framework in [atlan-postgres-app](https://github.com/atlanhq/atlan-postgres-app/tree/main/tests/e2e/test_postgres_workflow).

```bash
pytest tests/e2e/test_postgres_workflow
```

Each config file corresponds to a test scenario, if you want to add more test scenarios, you can just copy the directory and change the config file to test the new scenario.

## Order of Execution

The tests are executed in the following order:

1. `test_health_check`
2. `test_auth`
3. `test_metadata`
4. `test_preflight_check`
5. `test_run_workflow`
6. `test_data_validation`

This order is specified using the `@pytest.mark.order` decorator. If you are creating a test class that has more steps, you need to decorate the test methods with the `@pytest.mark.order` decorator to ensure they run in the correct order.

The value in the `@pytest.mark.order` decorator should be greater than **6** if you want them to run after the `test_data_validation` method.

