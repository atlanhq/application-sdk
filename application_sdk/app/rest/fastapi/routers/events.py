# """Router for handling metric-related API endpoints."""

# from typing import List, Optional

# from fastapi import APIRouter, Depends, HTTPException, Request
# from opentelemetry.proto.metrics.v1.metrics_pb2 import MetricsData
# from sqlalchemy.orm import Session

# from application_sdk.app.database import get_session
# from application_sdk.app.rest.common.interfaces.metrics import Metrics
# from application_sdk.app.rest.common.models.telemetry import Metric

# router = APIRouter(
#     prefix="/events/v1",
#     tags=["events"],
#     responses={404: {"description": "Not found"}},
# )


# @router.get("/activity_start", response_model=dict)
# async def activity_start(
#     event
# ):
#     pass

# @router.get("/activity_end", response_model=dict)
# async def activity_end(
#     event
# ):
#     pass

# @router.get("/workflow_start", response_model=dict)
# async def workflow_start(
#     event
# ):
#     pass

# @router.get("/workflow_end", response_model=dict)
# async def workflow_end(
#     *args, **kwargs
# ):
#     pass

# def build_events_router(fast_api_app: FastAPIApplication) -> APIRouter:
#     return router
