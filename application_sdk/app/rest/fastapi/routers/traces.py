from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from opentelemetry.proto.trace.v1.trace_pb2 import TracesData
from sqlalchemy.orm import Session

from application_sdk.app.rest.database import get_session
from application_sdk.paas.traces import Traces
from application_sdk.app.rest.schemas import Trace

router = APIRouter(
    prefix="/telemetry/v1/traces",
    tags=["traces"],
    responses={404: {"description": "Not found"}},
)


@router.get("", response_model=list[Trace])
async def read_traces(
    skip: int = 0,
    limit: int = 100,
    from_timestamp: int = 0,
    to_timestamp: Optional[int] = None,
    session: Session = Depends(get_session),
):
    return Traces.get_traces(session, skip, limit, from_timestamp, to_timestamp)


@router.post("", response_model=List[Trace])
async def create_trace(trace: Request, session: Session = Depends(get_session)):
    body = await trace.body()
    trace_message = TracesData()
    trace_message.ParseFromString(body)
    traces = Traces.create_traces(session, trace_message)
    return traces
