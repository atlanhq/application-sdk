"""
Azure utilities for the application-sdk framework.

This module provides common utility functions for Azure operations including
URL parsing, credential validation, and connection string generation.
"""

import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from application_sdk.common.error_codes import CommonError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def parse_azure_url(url: str) -> Dict[str, str]:
    """
    Parse Azure service URL to extract components.
    
    Args:
        url (str): Azure service URL
        
    Returns:
        Dict[str, str]: Parsed URL components
        
    Raises:
        CommonError: If URL is invalid or not an Azure URL
    """
    try:
        parsed = urlparse(url)
        
        if not parsed.scheme or not parsed.netloc:
            raise CommonError(f"{CommonError.CREDENTIALS_PARSE_ERROR}: Invalid URL format")
        
        # Extract service type from hostname
        hostname = parsed.netloc.lower()
        service_type = None
        
        if ".blob.core.windows.net" in hostname:
            service_type = "blob"
        elif ".dfs.core.windows.net" in hostname:
            service_type = "datalake"
        else:
            service_type = "unknown"
        
        # Extract account/resource name
        account_name = hostname.split('.')[0]
        
        return {
            "scheme": parsed.scheme,
            "hostname": hostname,
            "account_name": account_name,
            "service_type": service_type,
            "path": parsed.path,
            "query": parsed.query,
            "fragment": parsed.fragment
        }
        
    except Exception as e:
        logger.error(f"Failed to parse Azure URL: {str(e)}")
        raise CommonError(f"{CommonError.CREDENTIALS_PARSE_ERROR}: {str(e)}")


def validate_azure_credentials(credentials: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Validate Azure Service Principal credentials structure.
    
    Args:
        credentials (Dict[str, Any]): Azure Service Principal credentials to validate
        
    Returns:
        Tuple[bool, Optional[str]]: (is_valid, error_message)
    """
    try:
        auth_type = credentials.get("authType", "service_principal")
        
        if auth_type != "service_principal":
            return False, f"Only 'service_principal' authentication is supported. Received: {auth_type}"
        
        # Validate Service Principal credentials
        required_fields = ["tenant_id", "client_id", "client_secret"]
        for field in required_fields:
            if not credentials.get(field):
                return False, f"Missing required field: {field}"
        
        return True, None
        
    except Exception as e:
        return False, f"Validation error: {str(e)}"


def build_azure_connection_string(
    service_type: str,
    account_name: str,
    database: Optional[str] = None,
    port: Optional[int] = None,
    **kwargs: Any
) -> str:
    """
    Build Azure connection string for different services.
    
    Args:
        service_type (str): Type of Azure service
        account_name (str): Azure account/resource name
        database (Optional[str]): Database name (unused, kept for compatibility)
        port (Optional[int]): Port number (unused, kept for compatibility)
        **kwargs: Additional connection parameters
        
    Returns:
        str: Connection string
        
    Raises:
        CommonError: If service type is not supported
    """
    try:
        if service_type == "blob":
            connection_string = f"https://{account_name}.blob.core.windows.net"
        elif service_type == "datalake":
            connection_string = f"https://{account_name}.dfs.core.windows.net"
        else:
            raise CommonError(f"{CommonError.CREDENTIALS_PARSE_ERROR}: Unsupported service type: {service_type}")
        
        # Add additional parameters
        if kwargs:
            if "?" not in connection_string:
                connection_string += "?"
            else:
                connection_string += "&"
            
            param_pairs = [f"{key}={value}" for key, value in kwargs.items()]
            connection_string += "&".join(param_pairs)
        
        return connection_string
        
    except Exception as e:
        logger.error(f"Failed to build Azure connection string: {str(e)}")
        raise CommonError(f"{CommonError.CREDENTIALS_PARSE_ERROR}: {str(e)}")


def extract_azure_resource_info(resource_id: str) -> Dict[str, str]:
    """
    Extract information from Azure resource ID.
    
    Args:
        resource_id (str): Azure resource ID
        
    Returns:
        Dict[str, str]: Extracted resource information
        
    Raises:
        CommonError: If resource ID is invalid
    """
    try:
        # Azure resource ID format: /subscriptions/{subscription-id}/resourceGroups/{resource-group}/providers/{provider}/...
        parts = resource_id.strip('/').split('/')
        
        if len(parts) < 6:
            raise CommonError(f"{CommonError.CREDENTIALS_PARSE_ERROR}: Invalid resource ID format")
        
        result = {
            "subscription_id": parts[1],
            "resource_group": parts[3],
            "provider": parts[5],
        }
        
        # Extract resource type and name
        if len(parts) >= 8:
            result["resource_type"] = parts[6]
            result["resource_name"] = parts[7]
        
        # Extract additional path components
        if len(parts) > 8:
            result["additional_path"] = "/".join(parts[8:])
        
        return result
        
    except Exception as e:
        logger.error(f"Failed to extract Azure resource info: {str(e)}")
        raise CommonError(f"{CommonError.CREDENTIALS_PARSE_ERROR}: {str(e)}")


def validate_azure_permissions(permissions: list[str]) -> Tuple[bool, list[str]]:
    """
    Validate Azure permissions list.
    
    Args:
        permissions (list[str]): List of Azure permissions
        
    Returns:
        Tuple[bool, list[str]]: (is_valid, invalid_permissions)
    """
    try:
        # Common Azure permission patterns
        valid_patterns = [
            r"^Microsoft\.[A-Za-z]+/[A-Za-z]+/read$",
            r"^Microsoft\.[A-Za-z]+/[A-Za-z]+/write$",
            r"^Microsoft\.[A-Za-z]+/[A-Za-z]+/delete$",
            r"^Microsoft\.[A-Za-z]+/[A-Za-z]+/\*$",
            r"^Microsoft\.[A-Za-z]+/\*$",
            r"^\*$"
        ]
        
        invalid_permissions = []
        
        for permission in permissions:
            is_valid = False
            for pattern in valid_patterns:
                if re.match(pattern, permission):
                    is_valid = True
                    break
            
            if not is_valid:
                invalid_permissions.append(permission)
        
        return len(invalid_permissions) == 0, invalid_permissions
        
    except Exception as e:
        logger.error(f"Failed to validate Azure permissions: {str(e)}")
        return False, [str(e)]


def get_azure_service_endpoint(service_type: str, region: Optional[str] = None) -> str:
    """
    Get Azure service endpoint URL.
    
    Args:
        service_type (str): Type of Azure service
        region (Optional[str]): Azure region
        
    Returns:
        str: Service endpoint URL
        
    Raises:
        CommonError: If service type is not supported
    """
    try:
        endpoints = {
            "management": "https://management.azure.com",
            "resource_manager": "https://management.azure.com",
            "storage": "https://storage.azure.com",
        }
        
        if service_type not in endpoints:
            raise CommonError(f"{CommonError.CREDENTIALS_PARSE_ERROR}: Unsupported service type: {service_type}")
        
        endpoint = endpoints[service_type]
        
        # Add region-specific endpoint if provided
        if region and service_type == "storage":
            endpoint = f"https://{region}.storage.azure.com"
        
        return endpoint
        
    except Exception as e:
        logger.error(f"Failed to get Azure service endpoint: {str(e)}")
        raise CommonError(f"{CommonError.CREDENTIALS_PARSE_ERROR}: {str(e)}")


def format_azure_error_message(error: Exception, context: Optional[str] = None) -> str:
    """
    Format Azure error message with context.
    
    Args:
        error (Exception): Azure error exception
        context (Optional[str]): Additional context
        
    Returns:
        str: Formatted error message
    """
    try:
        error_message = str(error)
        
        # Extract error code if present
        error_code_match = re.search(r'ErrorCode=([^,\s]+)', error_message)
        error_code = error_code_match.group(1) if error_code_match else None
        
        # Extract status code if present
        status_match = re.search(r'Status Code: (\d+)', error_message)
        status_code = status_match.group(1) if status_match else None
        
        # Build formatted message
        formatted_parts = []
        
        if context:
            formatted_parts.append(f"Context: {context}")
        
        if error_code:
            formatted_parts.append(f"Error Code: {error_code}")
        
        if status_code:
            formatted_parts.append(f"Status Code: {status_code}")
        
        formatted_parts.append(f"Error: {error_message}")
        
        return " | ".join(formatted_parts)
        
    except Exception as e:
        logger.error(f"Failed to format Azure error message: {str(e)}")
        return f"Azure Error: {str(error)}" 