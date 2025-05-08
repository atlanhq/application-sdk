import json
import pytest
from hypothesis import given, strategies as st
from typing import Dict, Any

from application_sdk.credentials.credentials_utils import process_secret_data, apply_secret_values


# Helper strategy for credentials dictionaries
credential_dict_strategy = st.dictionaries(
    keys=st.text(min_size=1),
    values=st.one_of(st.text(), st.integers(), st.booleans()),
    min_size=1
)


# Tests for the utility functions
class TestCredentialUtils:
    @given(
        secret_data=st.dictionaries(
            keys=st.text(min_size=1),
            values=st.text(),
            min_size=1,
            max_size=10
        )
    )
    def test_process_secret_data_dict(self, secret_data: Dict[str, str]):
        result = process_secret_data(secret_data)
        assert result == secret_data
    
    def test_process_secret_data_json(self):
        nested_data = {"username": "test_user", "password": "test_pass"}
        secret_data = {"data": json.dumps(nested_data)}
        
        result = process_secret_data(secret_data)
        assert result == nested_data
    
    @given(
        source_credentials=credential_dict_strategy,
        secret_data=credential_dict_strategy
    )
    def test_apply_secret_values(self, source_credentials: Dict[str, Any], 
                                secret_data: Dict[str, Any]):
        # Create a copy of source credentials with values that exist in secret_data
        test_credentials = source_credentials.copy()
        
        # Add some keys that should be substituted
        secret_keys = list(secret_data.keys())
        for i in range(min(2, len(secret_keys))):
            if i < len(secret_keys):
                key = secret_keys[i]
                if key in secret_data:
                    test_credentials[f"test_{key}"] = key
        
        # Add an extra field with some substitutions
        extra_dict = {}
        for i in range(min(2, len(secret_keys))):
            if i < len(secret_keys):
                key = secret_keys[i]
                extra_dict[f"extra_{key}"] = key
        
        test_credentials["extra"] = extra_dict
        
        # Apply the substitutions
        result = apply_secret_values(test_credentials, secret_data)
        
        # Check that substitutions were made correctly
        for key, value in result.items():
            if key != "extra" and key.startswith("test_") and isinstance(value, str) and value in secret_data:
                assert result[key] == secret_data[value]
                
        # Check extra dict substitutions
        if "extra" in result:
            for key, value in result["extra"].items():
                if key.startswith("extra_") and isinstance(value, str) and value in secret_data:
                    assert result["extra"][key] == secret_data[value]