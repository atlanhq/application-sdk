"""Back-compatibility tests: every legacy error class is importable from its original location.

Covers:
- All import paths (module-level and __init__ re-exports)
- isinstance hierarchy: every shim is an AppError subclass
- DeprecationWarning emitted where expected
- Constructor signatures still accept their original positional/keyword arguments
- Legacy ErrorCode constants still importable with correct AAF-* codes
- AtlanError namespace classes still importable and carry their ErrorCode attributes
"""

import warnings

import pytest

from application_sdk.errors.base import AppError
from application_sdk.errors.leaves import (
    AuthError,
    DependencyUnavailableError,
    InternalError,
    InvalidInputError,
    NotFoundError,
)

# =============================================================================
# application_sdk.app — AppError, NonRetryableError, RetryableError, AppContextError
# =============================================================================


def test_app_error_importable_from_app_base() -> None:
    from application_sdk.app.base import AppError as LegacyAppError  # noqa: F401


def test_app_error_importable_from_app_init() -> None:
    from application_sdk.app import AppError as LegacyAppError  # noqa: F401


def test_app_error_is_app_error_subclass() -> None:
    from application_sdk.app.base import AppError as LegacyAppError

    assert issubclass(LegacyAppError, AppError)


def test_non_retryable_error_importable_from_app_base() -> None:
    from application_sdk.app.base import NonRetryableError  # noqa: F401


def test_non_retryable_error_importable_from_app_init() -> None:
    from application_sdk.app import NonRetryableError  # noqa: F401


def test_retryable_error_importable_from_app_base() -> None:
    from application_sdk.app.base import RetryableError  # noqa: F401


def test_retryable_error_importable_from_app_init() -> None:
    from application_sdk.app import RetryableError  # noqa: F401


def test_app_context_error_importable_from_app_base() -> None:
    from application_sdk.app.base import AppContextError  # noqa: F401


def test_non_retryable_is_app_error_not_retryable() -> None:
    from application_sdk.app.base import NonRetryableError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = NonRetryableError("boom")
    assert isinstance(e, AppError)
    assert isinstance(e, Exception)
    assert e.effective_retryable is False


def test_retryable_is_app_error_and_retryable() -> None:
    from application_sdk.app.base import RetryableError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = RetryableError("transient")
    assert isinstance(e, AppError)
    assert e.effective_retryable is True


def test_app_context_error_is_app_error_and_internal() -> None:
    from application_sdk.app.base import AppContextError

    e = AppContextError("called outside context")
    assert isinstance(e, AppError)
    assert isinstance(e, InternalError)
    assert isinstance(e, Exception)
    assert e.effective_retryable is False


def test_app_context_error_no_longer_is_runtime_error() -> None:
    from application_sdk.app.base import AppContextError

    e = AppContextError("called outside context")
    assert not isinstance(e, RuntimeError)


def test_legacy_app_error_accepts_positional_message() -> None:
    from application_sdk.app.base import AppError as LegacyAppError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = LegacyAppError("positional message")
    assert e.message == "positional message"
    assert str(e) != ""


def test_legacy_app_error_emits_deprecation_warning() -> None:
    from application_sdk.app.base import AppError as LegacyAppError

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        LegacyAppError("test")
    assert any(issubclass(x.category, DeprecationWarning) for x in w)


def test_non_retryable_emits_deprecation_warning() -> None:
    from application_sdk.app.base import NonRetryableError

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        NonRetryableError("test")
    assert any(issubclass(x.category, DeprecationWarning) for x in w)


def test_retryable_emits_deprecation_warning() -> None:
    from application_sdk.app.base import RetryableError

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        RetryableError("test")
    assert any(issubclass(x.category, DeprecationWarning) for x in w)


# =============================================================================
# application_sdk.app.entrypoint / registry / task — internal app errors
# =============================================================================


def test_entrypoint_contract_error_importable() -> None:
    from application_sdk.app.entrypoint import EntryPointContractError  # noqa: F401


def test_entrypoint_contract_error_is_app_error() -> None:
    from application_sdk.app.entrypoint import EntryPointContractError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = EntryPointContractError("bad entrypoint")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)


def test_app_not_found_error_importable() -> None:
    from application_sdk.app.registry import AppNotFoundError  # noqa: F401


def test_app_not_found_error_is_app_error() -> None:
    from application_sdk.app.registry import AppNotFoundError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = AppNotFoundError("my-app")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)


def test_app_already_registered_error_importable() -> None:
    from application_sdk.app.registry import AppAlreadyRegisteredError  # noqa: F401


def test_app_already_registered_error_is_app_error() -> None:
    from application_sdk.app.registry import AppAlreadyRegisteredError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = AppAlreadyRegisteredError("my-app", "1.0")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)


def test_task_not_found_error_importable() -> None:
    from application_sdk.app.registry import TaskNotFoundError  # noqa: F401


def test_task_not_found_error_is_app_error() -> None:
    from application_sdk.app.registry import TaskNotFoundError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = TaskNotFoundError("my-app", "my-task")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)


def test_task_contract_error_importable() -> None:
    from application_sdk.app.task import TaskContractError  # noqa: F401


def test_task_contract_error_is_app_error() -> None:
    from application_sdk.app.task import TaskContractError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = TaskContractError("bad task contract")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)


# =============================================================================
# application_sdk.credentials — CredentialError, CredentialNotFoundError, etc.
# =============================================================================


def test_credential_error_importable_from_errors() -> None:
    from application_sdk.credentials.errors import CredentialError  # noqa: F401


def test_credential_error_importable_from_init() -> None:
    from application_sdk.credentials import CredentialError  # noqa: F401


def test_credential_error_is_app_error() -> None:
    from application_sdk.credentials.errors import CredentialError

    e = CredentialError("cred failed")
    assert isinstance(e, AppError)
    assert isinstance(e, AuthError)


def test_credential_not_found_importable_from_errors() -> None:
    from application_sdk.credentials.errors import CredentialNotFoundError  # noqa: F401


def test_credential_not_found_importable_from_init() -> None:
    from application_sdk.credentials import CredentialNotFoundError  # noqa: F401


def test_credential_not_found_is_app_error() -> None:
    from application_sdk.credentials.errors import (
        CredentialError,
        CredentialNotFoundError,
    )
    from application_sdk.errors.categories import FailureCategory

    e = CredentialNotFoundError("my-cred")
    assert isinstance(e, AppError)
    assert isinstance(e, NotFoundError)
    assert isinstance(e, CredentialError)
    # AuthError still in MRO via CredentialError — broad except blocks keep catching
    assert isinstance(e, AuthError)
    assert isinstance(e, Exception)
    assert e.category == FailureCategory.NOT_FOUND


def test_credential_not_found_accepts_positional_name() -> None:
    from application_sdk.credentials.errors import CredentialNotFoundError

    e = CredentialNotFoundError("my-cred-guid")
    assert "my-cred-guid" in str(e)


def test_credential_parse_error_importable_from_errors() -> None:
    from application_sdk.credentials.errors import CredentialParseError  # noqa: F401


def test_credential_parse_error_importable_from_init() -> None:
    from application_sdk.credentials import CredentialParseError  # noqa: F401


def test_credential_parse_error_is_app_error() -> None:
    from application_sdk.credentials.errors import CredentialError, CredentialParseError

    e = CredentialParseError("could not parse")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)
    assert isinstance(e, CredentialError)


def test_credential_validation_error_importable_from_errors() -> None:
    from application_sdk.credentials.errors import (  # noqa: F401
        CredentialValidationError,
    )


def test_credential_validation_error_importable_from_init() -> None:
    from application_sdk.credentials import CredentialValidationError  # noqa: F401


def test_credential_validation_error_is_app_error() -> None:
    from application_sdk.credentials.errors import (
        CredentialError,
        CredentialValidationError,
    )

    e = CredentialValidationError("invalid cred")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)
    assert isinstance(e, CredentialError)


def test_oauth_token_error_importable_from_oauth() -> None:
    from application_sdk.credentials.oauth import OAuthTokenError  # noqa: F401


def test_oauth_token_error_importable_from_init() -> None:
    from application_sdk.credentials import OAuthTokenError  # noqa: F401


def test_oauth_token_error_is_app_error() -> None:
    from application_sdk.credentials.oauth import OAuthTokenError

    e = OAuthTokenError(message="token expired")
    assert isinstance(e, AppError)
    assert isinstance(e, AuthError)


# =============================================================================
# application_sdk.storage — StorageError, StorageNotFoundError, etc.
# =============================================================================


def test_storage_error_importable_from_errors() -> None:
    from application_sdk.storage.errors import StorageError  # noqa: F401


def test_storage_error_importable_from_init() -> None:
    from application_sdk.storage import StorageError  # noqa: F401


def test_storage_error_is_app_error() -> None:
    from application_sdk.storage.errors import StorageError

    e = StorageError("bucket gone")
    assert isinstance(e, AppError)
    assert isinstance(e, DependencyUnavailableError)


def test_storage_not_found_importable_from_errors() -> None:
    from application_sdk.storage.errors import StorageNotFoundError  # noqa: F401


def test_storage_not_found_importable_from_init() -> None:
    from application_sdk.storage import StorageNotFoundError  # noqa: F401


def test_storage_not_found_is_app_error() -> None:
    from application_sdk.errors.categories import FailureCategory
    from application_sdk.storage.errors import StorageError, StorageNotFoundError

    e = StorageNotFoundError("no such key")
    assert isinstance(e, AppError)
    assert isinstance(e, NotFoundError)
    assert isinstance(e, StorageError)
    assert e.category == FailureCategory.NOT_FOUND


def test_storage_permission_error_importable_from_errors() -> None:
    from application_sdk.storage.errors import StoragePermissionError  # noqa: F401


def test_storage_permission_error_importable_from_init() -> None:
    from application_sdk.storage import StoragePermissionError  # noqa: F401


def test_storage_permission_error_is_app_error() -> None:
    from application_sdk.errors.leaves import AppPermissionDeniedError
    from application_sdk.storage.errors import StorageError, StoragePermissionError

    e = StoragePermissionError("access denied")
    assert isinstance(e, AppError)
    assert isinstance(e, AppPermissionDeniedError)
    assert isinstance(e, StorageError)


def test_storage_config_error_importable_from_errors() -> None:
    from application_sdk.storage.errors import StorageConfigError  # noqa: F401


def test_storage_config_error_importable_from_init() -> None:
    from application_sdk.storage import StorageConfigError  # noqa: F401


def test_storage_config_error_is_app_error() -> None:
    from application_sdk.storage.errors import StorageConfigError, StorageError

    e = StorageConfigError("bad config")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)
    assert isinstance(e, StorageError)


# =============================================================================
# application_sdk.infrastructure — secrets, state, pubsub, bindings, vault
# =============================================================================


def test_secret_store_error_importable_from_secrets() -> None:
    from application_sdk.infrastructure.secrets import SecretStoreError  # noqa: F401


def test_secret_store_error_importable_from_init() -> None:
    from application_sdk.infrastructure import SecretStoreError  # noqa: F401


def test_secret_store_error_is_app_error() -> None:
    from application_sdk.infrastructure.secrets import SecretStoreError

    e = SecretStoreError("store unavailable")
    assert isinstance(e, AppError)
    assert isinstance(e, DependencyUnavailableError)


def test_secret_not_found_importable_from_secrets() -> None:
    from application_sdk.infrastructure.secrets import SecretNotFoundError  # noqa: F401


def test_secret_not_found_importable_from_init() -> None:
    from application_sdk.infrastructure import SecretNotFoundError  # noqa: F401


def test_secret_not_found_is_app_error() -> None:
    from application_sdk.errors.categories import FailureCategory
    from application_sdk.infrastructure.secrets import (
        SecretNotFoundError,
        SecretStoreError,
    )

    e = SecretNotFoundError("MY_SECRET")
    assert isinstance(e, AppError)
    assert isinstance(e, NotFoundError)
    assert isinstance(e, SecretStoreError)
    assert e.category == FailureCategory.NOT_FOUND


def test_secret_not_found_accepts_positional_name() -> None:
    from application_sdk.infrastructure.secrets import SecretNotFoundError

    e = SecretNotFoundError("DB_PASSWORD")
    assert "DB_PASSWORD" in str(e)


def test_state_store_error_importable_from_state() -> None:
    from application_sdk.infrastructure.state import StateStoreError  # noqa: F401


def test_state_store_error_importable_from_init() -> None:
    from application_sdk.infrastructure import StateStoreError  # noqa: F401


def test_state_store_error_is_app_error() -> None:
    from application_sdk.infrastructure.state import StateStoreError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = StateStoreError("state unavailable")
    assert isinstance(e, AppError)
    assert isinstance(e, DependencyUnavailableError)


def test_pubsub_error_importable_from_pubsub() -> None:
    from application_sdk.infrastructure.pubsub import PubSubError  # noqa: F401


def test_pubsub_error_importable_from_init() -> None:
    from application_sdk.infrastructure import PubSubError  # noqa: F401


def test_pubsub_error_is_app_error() -> None:
    from application_sdk.infrastructure.pubsub import PubSubError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = PubSubError("pubsub down")
    assert isinstance(e, AppError)
    assert isinstance(e, DependencyUnavailableError)


def test_binding_error_importable_from_bindings() -> None:
    from application_sdk.infrastructure.bindings import BindingError  # noqa: F401


def test_binding_error_importable_from_init() -> None:
    from application_sdk.infrastructure import BindingError  # noqa: F401


def test_binding_error_is_app_error() -> None:
    from application_sdk.infrastructure.bindings import BindingError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = BindingError("binding failed")
    assert isinstance(e, AppError)
    assert isinstance(e, DependencyUnavailableError)


def test_credential_vault_error_importable_from_vault() -> None:
    from application_sdk.infrastructure.credential_vault import (  # noqa: F401
        CredentialVaultError,
    )


def test_credential_vault_error_importable_from_init() -> None:
    from application_sdk.infrastructure import CredentialVaultError  # noqa: F401


def test_credential_vault_error_is_app_error() -> None:
    from application_sdk.infrastructure.credential_vault import CredentialVaultError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = CredentialVaultError("vault unreachable")
    assert isinstance(e, AppError)
    assert isinstance(e, DependencyUnavailableError)


# =============================================================================
# application_sdk.handler — HandlerError
# =============================================================================


def test_handler_error_importable_from_base() -> None:
    from application_sdk.handler.base import HandlerError  # noqa: F401


def test_handler_error_importable_from_init() -> None:
    from application_sdk.handler import HandlerError  # noqa: F401


def test_handler_error_is_app_error() -> None:
    from application_sdk.handler.base import HandlerError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = HandlerError("handler exploded")
    assert isinstance(e, AppError)
    assert isinstance(e, Exception)


def test_handler_error_accepts_positional_message() -> None:
    from application_sdk.handler.base import HandlerError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = HandlerError("positional")
    assert "positional" in str(e)


# =============================================================================
# application_sdk.contracts — ContractValidationError, PayloadSafetyError
# =============================================================================


def test_contract_validation_error_importable_from_base() -> None:
    from application_sdk.contracts.base import ContractValidationError  # noqa: F401


def test_contract_validation_error_importable_from_init() -> None:
    from application_sdk.contracts import ContractValidationError  # noqa: F401


def test_contract_validation_error_is_app_error() -> None:
    from application_sdk.contracts.base import ContractValidationError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = ContractValidationError("validation failed")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)


def test_payload_safety_error_importable_from_base() -> None:
    from application_sdk.contracts.base import PayloadSafetyError  # noqa: F401


def test_payload_safety_error_importable_from_init() -> None:
    from application_sdk.contracts import PayloadSafetyError  # noqa: F401


def test_payload_safety_error_is_app_error() -> None:
    from application_sdk.contracts.base import PayloadSafetyError

    e = PayloadSafetyError("MyModel", "data", bytes, "bytes not allowed")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)


def test_payload_safety_error_str_contains_field_name() -> None:
    from application_sdk.contracts.base import PayloadSafetyError

    e = PayloadSafetyError("MyModel", "my_field", bytes, "bytes not allowed")
    assert "my_field" in str(e)


# =============================================================================
# application_sdk.discovery — DiscoveryError
# =============================================================================


def test_discovery_error_importable() -> None:
    from application_sdk.discovery import DiscoveryError  # noqa: F401


def test_discovery_error_is_app_error() -> None:
    from application_sdk.discovery import DiscoveryError

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        e = DiscoveryError("module not found")
    assert isinstance(e, AppError)
    assert isinstance(e, InvalidInputError)


# =============================================================================
# application_sdk.common.error_codes — AtlanError namespace classes
# =============================================================================


def test_atlan_error_importable() -> None:
    from application_sdk.common.error_codes import AtlanError  # noqa: F401


def test_client_error_importable() -> None:
    from application_sdk.common.error_codes import ClientError  # noqa: F401


def test_api_error_importable() -> None:
    from application_sdk.common.error_codes import ApiError  # noqa: F401


def test_orchestrator_error_importable() -> None:
    from application_sdk.common.error_codes import OrchestratorError  # noqa: F401


def test_workflow_error_importable() -> None:
    from application_sdk.common.error_codes import WorkflowError  # noqa: F401


def test_io_error_importable() -> None:
    from application_sdk.common.error_codes import IOError  # noqa: F401


def test_common_error_importable() -> None:
    from application_sdk.common.error_codes import CommonError  # noqa: F401


def test_docgen_error_importable() -> None:
    from application_sdk.common.error_codes import DocGenError  # noqa: F401


def test_activity_error_importable() -> None:
    from application_sdk.common.error_codes import ActivityError  # noqa: F401


def test_atlan_error_namespace_classes_carry_error_code_attributes() -> None:
    from application_sdk.common.error_codes import (
        ActivityError,
        ApiError,
        ClientError,
        CommonError,
        DocGenError,
        IOError,
        OrchestratorError,
        WorkflowError,
    )

    assert ClientError.REQUEST_VALIDATION_ERROR is not None
    assert ApiError.SERVER_START_ERROR is not None
    assert OrchestratorError.ORCHESTRATOR_CLIENT_CONNECTION_ERROR is not None
    assert WorkflowError.WORKFLOW_EXECUTION_ERROR is not None
    assert IOError.INPUT_ERROR is not None
    assert CommonError.AWS_REGION_ERROR is not None
    assert DocGenError.DOCGEN_ERROR is not None
    assert ActivityError.ACTIVITY_START_ERROR is not None


def test_atlan_error_emits_deprecation_when_instantiated() -> None:
    from application_sdk.common.error_codes import AtlanError

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        AtlanError("test")
    assert any(issubclass(x.category, DeprecationWarning) for x in w)


def test_atlan_error_subclass_emits_deprecation_when_instantiated() -> None:
    from application_sdk.common.error_codes import ClientError

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        ClientError("test")
    assert any(issubclass(x.category, DeprecationWarning) for x in w)


# =============================================================================
# application_sdk.errors — legacy ErrorCode constants
# =============================================================================


def test_all_legacy_error_code_constants_importable() -> None:
    from application_sdk.errors import (  # noqa: F401
        APP_ALREADY_REGISTERED,
        APP_CONTEXT_ERROR,
        APP_ERROR,
        APP_NON_RETRYABLE,
        APP_NOT_FOUND,
        BINDING_ERROR,
        CONTRACT_VALIDATION,
        CREDENTIAL_ERROR,
        CREDENTIAL_NOT_FOUND,
        CREDENTIAL_PARSE_ERROR,
        CREDENTIAL_VALIDATION_ERROR,
        CREDENTIAL_VAULT_ERROR,
        DISCOVERY_ERROR,
        EVENT_BUS,
        EVENT_PUBLISH,
        EXECUTION_ACTIVITY_ERROR,
        EXECUTION_ERROR,
        EXECUTION_WORKER_ERROR,
        HANDLER_ERROR,
        PAYLOAD_SAFETY,
        PUBSUB_ERROR,
        SECRET_NOT_FOUND,
        SECRET_STORE_ERROR,
        SEGMENT_ERROR,
        STATE_STORE_ERROR,
        STORAGE_CONFIG,
        STORAGE_NOT_FOUND,
        STORAGE_OPERATION,
        STORAGE_PERMISSION,
        TASK_NOT_FOUND,
        ErrorCode,
    )


def test_legacy_error_code_aaf_format() -> None:
    from application_sdk.errors import (
        APP_ERROR,
        CREDENTIAL_NOT_FOUND,
        SECRET_NOT_FOUND,
        STORAGE_NOT_FOUND,
        ErrorCode,
    )

    assert APP_ERROR.code == "AAF-APP-001"
    assert CREDENTIAL_NOT_FOUND.code == "AAF-CRD-002"
    assert SECRET_NOT_FOUND.code == "AAF-INF-005"
    assert STORAGE_NOT_FOUND.code == "AAF-STR-001"
    assert isinstance(APP_ERROR, ErrorCode)


# =============================================================================
# Catch-by-base-class: legacy errors are catchable as AppError
# =============================================================================


@pytest.mark.parametrize(
    "import_path,cls_name,construct",
    [
        (
            "application_sdk.app.base",
            "NonRetryableError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.app.base",
            "RetryableError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.app.base",
            "AppContextError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.credentials.errors",
            "CredentialError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.credentials.errors",
            "CredentialNotFoundError",
            lambda cls: cls("my-cred"),
        ),
        (
            "application_sdk.credentials.errors",
            "CredentialParseError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.credentials.errors",
            "CredentialValidationError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.storage.errors",
            "StorageError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.storage.errors",
            "StorageNotFoundError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.storage.errors",
            "StoragePermissionError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.storage.errors",
            "StorageConfigError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.infrastructure.secrets",
            "SecretStoreError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.infrastructure.secrets",
            "SecretNotFoundError",
            lambda cls: cls("MY_SECRET"),
        ),
        (
            "application_sdk.infrastructure.state",
            "StateStoreError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.infrastructure.pubsub",
            "PubSubError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.infrastructure.bindings",
            "BindingError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.infrastructure.credential_vault",
            "CredentialVaultError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.handler.base",
            "HandlerError",
            lambda cls: cls("msg"),
        ),
        (
            "application_sdk.discovery",
            "DiscoveryError",
            lambda cls: cls("msg"),
        ),
    ],
)
def test_legacy_error_catchable_as_app_error(
    import_path: str, cls_name: str, construct
) -> None:
    import importlib

    mod = importlib.import_module(import_path)
    cls = getattr(mod, cls_name)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        exc = construct(cls)
    try:
        raise exc
    except AppError:
        pass
    except Exception:
        pytest.fail(f"{cls_name} from {import_path} was not caught as AppError")
