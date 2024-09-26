from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from opentelemetry.proto.metrics.v1.metrics_pb2 import MetricsData
from sqlalchemy.orm import Session

from application_sdk.database import get_session
from application_sdk.interfaces.metrics import Metrics
from application_sdk.schemas import Metric

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
    return Metrics.get_metrics(session, from_timestamp, to_timestamp)


@router.post("", response_model=List[Metric])
async def create_metrics(request: Request, session: Session = Depends(get_session)):
    body = await request.body()
    metric_message = MetricsData()
    metric_message.ParseFromString(body)
    return Metrics.create_metrics(session, metric_message)
