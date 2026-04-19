"""Custom FastAPI endpoints example using v3 Handler pattern.

Demonstrates how to add custom HTTP endpoints alongside the standard
handler routes. In v3, the Handler is served via create_app_handler_service()
which returns a FastAPI app. You can add custom routes to that app.

Usage:
    python examples/application_custom_fastapi.py

    # Then test:
    curl http://localhost:8000/custom/test
    curl http://localhost:8000/health
"""

import asyncio

from application_sdk.app import App, Input, Output, task
from application_sdk.handler import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    Handler,
    MetadataInput,
    MetadataOutput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.main import run_dev_combined
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class SampleInput(Input):
    """Input for the sample workflow."""

    name: str = ""


class SampleOutput(Output):
    """Output from the sample workflow."""

    status: str = "completed"


class CustomHandler(Handler):
    """Handler with standard auth/preflight/metadata endpoints.

    Custom routes are added to the FastAPI app returned by
    create_app_handler_service() -- see the __main__ block below.
    """

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(
            status=PreflightStatus.READY,
            checks=[
                PreflightCheck(
                    name="connectionCheck",
                    passed=True,
                    message="Connection verified",
                ),
            ],
        )

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(objects=[])


class SampleApp(App):
    """Simple app paired with the custom handler."""

    @task(timeout_seconds=30)
    async def process(self, input: SampleInput) -> SampleOutput:
        return SampleOutput(status="completed")

    async def run(self, input: SampleInput) -> SampleOutput:  # type: ignore[override]
        return await self.process(input)


if __name__ == "__main__":
    # For adding truly custom routes, create the FastAPI app directly:
    #
    #   from fastapi import APIRouter
    #   handler = CustomHandler()
    #   fastapi_app = create_app_handler_service(handler, app_name="custom-app")
    #   custom_router = APIRouter(prefix="/custom")
    #
    #   @custom_router.get("/test")
    #   async def test_endpoint():
    #       return {"message": "Hello, World!"}
    #
    #   fastapi_app.include_router(custom_router)
    #   uvicorn.run(fastapi_app, host="0.0.0.0", port=8000)
    #
    # For the common case (standard handler + worker), use run_dev_combined:
    asyncio.run(
        run_dev_combined(
            SampleApp,
            example_input={"name": "test"},
        )
    )
