class ApplicationFrameworkErrorCodes:
    """
    Error Code Structure: APPXYYZ
    APP: Application prefix
    X: Component identifier (1-9)
    Y: Sub-component/type identifier
    Z: Specific error type
    """

    class ActivityErrorCodes:
        """
        Error codes for activity-related errors.
        Code Type : APP1YZZ
        X: 1 for activity
        Y: 1-9 for different activity types
        Z: Specific error type
        """

        # General Activity Errors
        ACTIVITY_START_ERROR = "APP1101"
        ACTIVITY_END_ERROR = "APP1102"

        # Query Extraction Activity Errors (Y=2)
        QUERY_EXTRACTION_ERROR = "APP1201"
        QUERY_EXTRACTION_SQL_ERROR = "APP1202"
        QUERY_EXTRACTION_PARSE_ERROR = "APP1203"
        QUERY_EXTRACTION_VALIDATION_ERROR = "APP1204"

        # Metadata Extraction Activity Errors (Y=3)
        METADATA_EXTRACTION_ERROR = "APP1301"
        METADATA_EXTRACTION_SQL_ERROR = "APP1302"
        METADATA_EXTRACTION_REST_ERROR = "APP1303"
        METADATA_EXTRACTION_PARSE_ERROR = "APP1304"
        METADATA_EXTRACTION_VALIDATION_ERROR = "APP1305"

    class ApplicationErrorCodes:
        """
        Error codes for application-related errors.
        Code Type : APP2YZZ
        X: 2 for application
        Y: 1-9 for different application types
        Z: Specific error type
        """

        # Request Processing Errors (Z=1)
        REQUEST_PROCESSING_ERROR = "APP2111"
        REQUEST_VALIDATION_ERROR = "APP2112"
        REQUEST_AUTHENTICATION_ERROR = "APP2113"

        # Middleware Errors (Z=2)
        MIDDLEWARE_ERROR = "APP2221"
        LOG_MIDDLEWARE_ERROR = "APP2222"

        # Route Handler Errors (Z=3)
        ROUTE_HANDLER_ERROR = "APP2231"
        ENDPOINT_ERROR = "APP2232"

        # Server Errors (Z=4)
        SERVER_START_ERROR = "APP2241"
        SERVER_SHUTDOWN_ERROR = "APP2242"
        SERVER_CONFIG_ERROR = "APP4403"

        # Event Processing Errors (Z=5)
        EVENT_PROCESSING_ERROR = "APP2251"
        EVENT_VALIDATION_ERROR = "APP2252"
        EVENT_TRIGGER_ERROR = "APP2253"

    class ClientErrorCodes:
        """
        Error codes for client-related errors.
        Code Type : APP3YZZ
        X: 3 for client
        Y: 1-9 for different client types
        Z: Specific error type
        """

        # General Client Errors (Y=1)
        CLIENT_INIT_ERROR = "APP3101"
        CLIENT_CLOSE_ERROR = "APP3102"
        CLIENT_CONNECTION_ERROR = "APP3103"
        CLIENT_NOT_LOADED_ERROR = "APP3104"

        # SQL Client Errors (Y=2)
        SQL_CLIENT_LOAD_ERROR = "APP3201"
        SQL_CLIENT_QUERY_ERROR = "APP3202"
        SQL_CLIENT_AUTH_ERROR = "APP3203"
        SQL_CLIENT_CONNECTION_ERROR = "APP3204"

        # Workflow Client Errors (Y=3)
        WORKFLOW_CLIENT_START_ERROR = "APP3301"
        WORKFLOW_CLIENT_STOP_ERROR = "APP3302"
        WORKFLOW_CLIENT_STATUS_ERROR = "APP3303"
        WORKFLOW_CLIENT_WORKER_ERROR = "APP3304"
        WORKFLOW_CLIENT_NOT_FOUND_ERROR = "APP3305"

        # Temporal Client Errors (Y=4)
        TEMPORAL_CLIENT_CONNECTION_ERROR = "APP3401"
        TEMPORAL_CLIENT_WORKFLOW_ERROR = "APP3402"
        TEMPORAL_CLIENT_ACTIVITY_ERROR = "APP3403"
        TEMPORAL_CLIENT_WORKER_ERROR = "APP3404"

    class CommonErrorCodes:
        """
        Error codes for common errors.
        Code Type : APP4YZZ
        X: 4 for common
        Y: 1-9 for different common error types
        Z: Specific error type
        """

        # General Common Errors (Y=1)
        UNKNOWN_ERROR = "APP4101"
        CONFIGURATION_ERROR = "APP4102"
        VALIDATION_ERROR = "APP4103"

        # Logger Errors (Y=2)
        LOGGER_SETUP_ERROR = "APP4201"
        LOGGER_PROCESSING_ERROR = "APP4202"
        LOGGER_OTLP_ERROR = "APP4203"
        LOGGER_RESOURCE_ERROR = "APP4204"

        # AWS Utils Errors (Y=3)
        AWS_REGION_ERROR = "APP4301"
        AWS_ROLE_ERROR = "APP4302"
        AWS_CREDENTIALS_ERROR = "APP4303"
        AWS_TOKEN_ERROR = "APP4304"

        # Utils Errors (Y=4)
        QUERY_PREPARATION_ERROR = "APP4401"
        FILTER_PREPARATION_ERROR = "APP4402"
        CREDENTIALS_PARSE_ERROR = "APP4403"
        SQL_FILE_ERROR = "APP4404"

    class DocgenErrorCodes:
        """
        Error codes for docgen-related errors.
        Code Type : APP5YZZ
        X: 5 for docgen
        Y: 1-9 for different docgen error types
        Z: Specific error type
        """

        # General Docgen Errors (Y=1)
        DOCGEN_ERROR = "APP5101"
        DOCGEN_EXPORT_ERROR = "APP5102"
        DOCGEN_BUILD_ERROR = "APP5103"

        # Manifest Errors (Y=2)
        MANIFEST_NOT_FOUND_ERROR = "APP5201"
        MANIFEST_PARSE_ERROR = "APP5202"
        MANIFEST_VALIDATION_ERROR = "APP5203"
        MANIFEST_YAML_ERROR = "APP5204"

        # Directory Errors (Y=3)
        DIRECTORY_VALIDATION_ERROR = "APP5301"
        DIRECTORY_CONTENT_ERROR = "APP5302"
        DIRECTORY_STRUCTURE_ERROR = "APP5303"
        DIRECTORY_FILE_ERROR = "APP5304"

        # MkDocs Errors (Y=4)
        MKDOCS_CONFIG_ERROR = "APP5401"
        MKDOCS_EXPORT_ERROR = "APP5402"
        MKDOCS_NAV_ERROR = "APP5403"
        MKDOCS_BUILD_ERROR = "APP5404"

    class HandlerErrorCodes:
        """
        Error codes for handler-related errors.
        Code Type : APP6YZZ
        X: 6 for handler
        Y: 1-9 for different handler error types
        Z: Specific error type
        """

        # General Handler Errors (Y=1)
        HANDLER_ERROR = "APP6101"
        HANDLER_LOAD_ERROR = "APP6102"
        HANDLER_AUTH_ERROR = "APP6103"
        HANDLER_METADATA_ERROR = "APP6104"

        # SQL Handler Errors (Y=2)
        SQL_HANDLER_METADATA_ERROR = "APP6201"
        SQL_HANDLER_AUTH_ERROR = "APP6202"
        SQL_HANDLER_PREFLIGHT_ERROR = "APP6203"
        SQL_HANDLER_VERSION_ERROR = "APP6204"
        SQL_HANDLER_SCHEMA_ERROR = "APP6205"
        SQL_HANDLER_TABLE_ERROR = "APP6206"

        # Preflight Check Errors (Y=3)
        PREFLIGHT_CHECK_ERROR = "APP6301"
        PREFLIGHT_SCHEMA_ERROR = "APP6302"
        PREFLIGHT_TABLE_ERROR = "APP6303"
        PREFLIGHT_VERSION_ERROR = "APP6304"

        # Metadata Fetch Errors (Y=4)
        METADATA_FETCH_ERROR = "APP6401"
        METADATA_DATABASE_ERROR = "APP6402"
        METADATA_SCHEMA_ERROR = "APP6403"
        METADATA_FILTER_ERROR = "APP6404"

    class InputErrorCodes:
        """
        Error codes for input-related errors.
        Code Type : APP7YZZ
        X: 7 for input
        Y: 1-9 for different input types
        Z: Specific error type
        """

        # General Input Errors (Y=1)
        INPUT_ERROR = "APP7101"
        INPUT_LOAD_ERROR = "APP7102"
        INPUT_VALIDATION_ERROR = "APP7103"
        INPUT_PROCESSING_ERROR = "APP7104"

        # SQL Query Errors (Y=2)
        SQL_QUERY_ERROR = "APP7201"
        SQL_QUERY_BATCH_ERROR = "APP7202"
        SQL_QUERY_PANDAS_ERROR = "APP7203"
        SQL_QUERY_DAFT_ERROR = "APP7204"

        # JSON Errors (Y=3)
        JSON_READ_ERROR = "APP7301"
        JSON_BATCH_ERROR = "APP7302"
        JSON_DAFT_ERROR = "APP7303"
        JSON_DOWNLOAD_ERROR = "APP7304"

        # Parquet Errors (Y=4)
        PARQUET_READ_ERROR = "APP7401"
        PARQUET_BATCH_ERROR = "APP7402"
        PARQUET_DAFT_ERROR = "APP7403"
        PARQUET_VALIDATION_ERROR = "APP7404"

        # Iceberg Errors (Y=5)
        ICEBERG_READ_ERROR = "APP7501"
        ICEBERG_DAFT_ERROR = "APP7502"
        ICEBERG_TABLE_ERROR = "APP7503"

        # Object Store Errors (Y=6)
        OBJECT_STORE_ERROR = "APP7601"
        OBJECT_STORE_DOWNLOAD_ERROR = "APP7602"
        OBJECT_STORE_READ_ERROR = "APP7603"

        # State Store Errors (Y=7)
        STATE_STORE_ERROR = "APP7701"
        STATE_STORE_EXTRACT_ERROR = "APP7702"
        STATE_STORE_VALIDATION_ERROR = "APP7703"

    class OutputErrorCodes:
        """
        Error codes for output-related errors.
        Code Type : APP8YZZ
        X: 8 for output
        Y: 1-9 for different output types
        Z: Specific error type
        """

        # General Output Errors (Y=1)
        OUTPUT_ERROR = "APP8101"
        OUTPUT_WRITE_ERROR = "APP8102"
        OUTPUT_STATISTICS_ERROR = "APP8103"
        OUTPUT_VALIDATION_ERROR = "APP8104"

        # JSON Output Errors (Y=2)
        JSON_WRITE_ERROR = "APP8201"
        JSON_BATCH_WRITE_ERROR = "APP8202"
        JSON_DAFT_WRITE_ERROR = "APP8203"

        # Parquet Output Errors (Y=3)
        PARQUET_WRITE_ERROR = "APP8301"
        PARQUET_DAFT_WRITE_ERROR = "APP8302"
        PARQUET_VALIDATION_ERROR = "APP8303"

        # Iceberg Output Errors (Y=4)
        ICEBERG_WRITE_ERROR = "APP8401"
        ICEBERG_DAFT_WRITE_ERROR = "APP8402"
        ICEBERG_TABLE_ERROR = "APP8403"

        # Object Store Errors (Y=5)
        OBJECT_STORE_ERROR = "APP8501"
        OBJECT_STORE_READ_ERROR = "APP8502"
        OBJECT_STORE_WRITE_ERROR = "APP8503"

        # State Store Errors (Y=6)
        STATE_STORE_ERROR = "APP8601"
        STATE_STORE_WRITE_ERROR = "APP8602"
        STATE_STORE_VALIDATION_ERROR = "APP8603"

    class TransformationErrorCodes:
        """
        Error codes for transformation-related errors.
        Code Type : APP9YZZ
        X: 9 for transformation
        Y: 1-9 for different transformation types
        Z: Specific error type
        """

        # General Transformation Errors (Y=1)
        TRANSFORMATION_ERROR = "APP9101"
        TRANSFORMATION_VALIDATION_ERROR = "APP9102"
        TRANSFORMATION_EXECUTION_ERROR = "APP9103"
        TRANSFORMATION_CONFIG_ERROR = "APP9104"

        # Atlas Transformation Errors (Y=2)
        ATLAS_TRANSFORMATION_ERROR = "APP9201"
        ATLAS_TYPE_ERROR = "APP9202"
        ATLAS_VALIDATION_ERROR = "APP9203"
        ATLAS_EXECUTION_ERROR = "APP9204"

        # Common Transformation Errors (Y=3)
        COMMON_TRANSFORMATION_ERROR = "APP9301"
        COMMON_VALIDATION_ERROR = "APP9302"
        COMMON_EXECUTION_ERROR = "APP9303"
        COMMON_CONFIG_ERROR = "APP9304"

    class WorkflowErrorCodes:
        """
        Error codes for workflow-related errors.
        Code Type : APP0YZZ
        X: 10 for workflow
        Y: 1-9 for different workflow types
        Z: Specific error type
        """

        # General Workflow Errors (Y=1)
        WORKFLOW_ERROR = "APP10101"
        WORKFLOW_EXECUTION_ERROR = "APP10102"
        WORKFLOW_VALIDATION_ERROR = "APP10103"
        WORKFLOW_CONFIG_ERROR = "APP10104"

        # Query Extraction Workflow Errors (Y=2)
        QUERY_EXTRACTION_ERROR = "APP10201"
        QUERY_EXTRACTION_EXECUTION_ERROR = "APP10202"
        QUERY_EXTRACTION_VALIDATION_ERROR = "APP10203"
        QUERY_EXTRACTION_CONFIG_ERROR = "APP10204"

        # Metadata Extraction Workflow Errors (Y=3)
        METADATA_EXTRACTION_ERROR = "APP10301"
        METADATA_EXTRACTION_EXECUTION_ERROR = "APP10302"
        METADATA_EXTRACTION_VALIDATION_ERROR = "APP10303"
        METADATA_EXTRACTION_CONFIG_ERROR = "APP10304"
