"""Router for handling log-related API endpoints."""

from typing import List, Optional
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from opentelemetry.proto.logs.v1.logs_pb2 import LogsData
from sqlalchemy.orm import Session

from application_sdk.app.database import get_session
from application_sdk.app.rest.interfaces.logs import Logs
from application_sdk.app.rest.schemas import Log

router = APIRouter(
    prefix="/telemetry/v1/logs",
    tags=["logs"],
    responses={404: {"description": "Not found"}},
)


@router.get("", response_model=list[Log])
async def read_logs(
    skip: int = 0,
    limit: int = 100,
    keyword: str = "",
    from_timestamp: int = 0,
    query_filters: str = "[]",
    to_timestamp: Optional[int] = None,
    session: Session = Depends(get_session),
):
    """
    Retrieve a list of logs.

    :param skip: Number of logs to skip (for pagination).
    :param limit: Maximum number of logs to return.
    :param keyword: Keyword to filter logs.
    :param from_timestamp: Start timestamp for log retrieval.
    :param to_timestamp: End timestamp for log retrieval.
    :param query_filters: Filters for logs.
    :param session: Database session.
    :return: A list of Log objects.
    :raises HTTPException: If there's an error with the database operations.
    """
    try:
        return Logs.get_logs(
            session, skip, limit, keyword, from_timestamp, to_timestamp, json.loads(query_filters)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("", response_model=List[Log])
async def create_logs(request: Request, session: Session = Depends(get_session)):
    """
    Create logs from a protobuf message.

    :param request: FastAPI request object.
    :param session: Database session.
    :return: A list of Log objects.
    :raises HTTPException: If there's an error with the database operations.
    """
    try:
        # Convert the request body to a protobuf message
        body = await request.body()
        log_message = LogsData()
        log_message.ParseFromString(body)
        return Logs.create_logs(session, log_message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
