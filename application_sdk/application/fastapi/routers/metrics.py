"""Router for handling metric-related API endpoints."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from opentelemetry.proto.metrics.v1.metrics_pb2 import MetricsData
from sqlalchemy.orm import Session

from application_sdk.app.database import get_session
from application_sdk.app.rest.common.interfaces.metrics import Metrics
from application_sdk.app.rest.common.models.telemetry import Metric

router = APIRouter(
    prefix="/telemetry/v1/metrics",
    tags=["metrics"],
    responses={404: {"description": "Not found"}},
)


@router.get("", response_model=dict)
async def read_metrics(
    from_timestamp: int = 0,
    to_timestamp: Optional[int] = None,
    session: Session = Depends(get_session),
):
    """
    Retrieve a list of metrics.

    :param from_timestamp: Start timestamp for metric retrieval.
    :param to_timestamp: End timestamp for metric retrieval.
    :param session: Database session.
    :return: A list of Metric objects.
    :raises HTTPException: If there's an error with the database operations.
    """
    try:
        return Metrics.get_metrics(session, from_timestamp, to_timestamp)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("", response_model=List[Metric])
async def create_metrics(request: Request, session: Session = Depends(get_session)):
    """
    Create metrics from a protobuf message.

    :param request: FastAPI request object.
    :param session: Database session.
    :return: A list of Metric objects.
    :raises HTTPException: If there's an error with the database operations.
    """
    try:
        body = await request.body()
        metric_message = MetricsData()
        metric_message.ParseFromString(body)
        return Metrics.create_metrics(session, metric_message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def get_metrics_router() -> APIRouter:
    return router
