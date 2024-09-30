"""Router for handling trace-related API endpoints."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from opentelemetry.proto.trace.v1.trace_pb2 import TracesData
from sqlalchemy.orm import Session

from application_sdk.app.database import get_session
from application_sdk.app.rest.interfaces.traces import Traces
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
    """
    Retrieve a list of traces.

    :param skip: Number of traces to skip (for pagination).
    :param limit: Maximum number of traces to return.
    :param from_timestamp: Start timestamp for trace retrieval.
    :param to_timestamp: End timestamp for trace retrieval.
    :param session: Database session.
    :return: A list of Trace objects.
    :raises HTTPException: If there's an error with the database operations.
    """
    try:
        return Traces.get_traces(session, skip, limit, from_timestamp, to_timestamp)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("", response_model=List[Trace])
async def create_trace(trace: Request, session: Session = Depends(get_session)):
    """
    Create traces from a protobuf message.

    :param trace: FastAPI request object.
    :param session: Database session.
    :return: A list of Trace objects.
    :raises HTTPException: If there's an error with the database operations.
    """
    try:
        body = await trace.body()
        trace_message = TracesData()
        trace_message.ParseFromString(body)
        traces = Traces.create_traces(session, trace_message)
        return traces
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
