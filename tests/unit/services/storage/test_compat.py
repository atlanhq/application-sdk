"""Backward compatibility tests for storage module imports."""


def test_import_from_old_path():
    """Old import path via objectstore.py shim still works."""
    from application_sdk.services.objectstore import ObjectStore as OldImport
    from application_sdk.services.storage import ObjectStore as NewImport

    assert OldImport is NewImport


def test_import_from_services():
    """Top-level services import works."""
    from application_sdk.services import ObjectStore
    from application_sdk.services.storage import ObjectStore as DirectImport

    assert ObjectStore is DirectImport


def test_objectstore_shim_all():
    """The shim module exposes ObjectStore in __all__."""
    import application_sdk.services.objectstore as shim

    assert "ObjectStore" in shim.__all__


def test_class_has_expected_methods():
    """ObjectStore exposes the expected public API."""
    from application_sdk.services.storage import ObjectStore

    expected_methods = [
        "as_store_key",
        "list_files",
        "get_content",
        "exists",
        "delete_file",
        "delete_prefix",
        "upload_file",
        "upload_file_from_bytes",
        "upload_prefix",
        "download_file",
        "download_prefix",
    ]
    for method in expected_methods:
        assert hasattr(ObjectStore, method), f"Missing method: {method}"


def test_class_has_expected_constants():
    """ObjectStore exposes the operation constants."""
    from application_sdk.services.storage import ObjectStore

    assert ObjectStore.OBJECT_CREATE_OPERATION == "create"
    assert ObjectStore.OBJECT_GET_OPERATION == "get"
    assert ObjectStore.OBJECT_LIST_OPERATION == "list"
    assert ObjectStore.OBJECT_DELETE_OPERATION == "delete"
