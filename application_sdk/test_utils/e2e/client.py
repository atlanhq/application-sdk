from typing import Any, Dict
from urllib.parse import urljoin

import requests


class APIServerClient:
    """
    Client for the API Server
    """

    def __init__(self, host: str, version: str = "v1"):
        self.host = host
        self.version = version
        self.base_url = urljoin(host, version)

    def test_connection(self, credentials: Dict) -> Dict[str, Any]:
        """
        Test Connection method

        Args:
            credentials (Dict): Credentials to test the connection
        Returns:
            Dict: Response from the test connection API
        """
        response = self._post("/auth", data=credentials)
        assert response.status_code == 200
        return response.json()

    def get_metadata(self, credentials: Dict) -> Dict[str, Any]:
        """
        Method for the /metadata API

        Args:
            credentials (Dict): Credentials for the metadata API
        Returns:
            Dict: Response from the metadata API
        """
        response = self._post("/metadata", data=credentials)
        assert response.status_code == 200
        return response.json()

    def preflight_check(self, credentials: Dict, metadata: Dict) -> Dict[str, Any]:
        """
        Method for the /check (preflight check) API

        Args:
            credentials (Dict): Credentials for the preflight check
            metadata (Dict): Metadata for the preflight check
        Returns:
            Dict: Response from the preflight check API
        """
        response = self._post(
            "/check", data={"credentials": credentials, "metadata": metadata}
        )
        assert response.status_code == 200
        return response.json()

    def run_workflow(self, data: Dict) -> Dict[str, Any]:
        """
        Method for the /start API to run the workflow

        Args:
            data (Dict): Data for the workflow start API
        Returns:
            Dict: Response from the workflow start API
        """
        response = self._post(
            "/start",
            data=data,
        )
        assert response.status_code == 200
        return response.json()

    def get_workflow_status(self, workflow_id: str, run_id: str) -> Dict[str, Any]:
        """
        Method to get the status of the workflow using /status API

        Args:
            workflow_id (str): Workflow ID
            run_id (str): Run ID
        Returns:
            Dict: Response from the status API
        """
        response = self._get(f"/status/{workflow_id}/{run_id}")
        assert response.status_code == 200
        return response.json()

    def _get(self, url: str = ""):
        """
        Method for the GET requests

        Args:
            url (str): URL to make the GET request
        Returns:
            Response: Response from the GET request
        """
        return requests.get(f"{self.base_url}{url}")

    def _post(self, url: str, data: Dict):
        """
        Method for the POST requests

        Args:
            url (str): URL to make the POST request
            data (Dict): Data to send in the POST request
        Returns:
            Response: Response from the POST request
        """
        return requests.post(f"{self.base_url}{url}", json=data)
