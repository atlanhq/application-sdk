"""Router for handling log-related API endpoints."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from opentelemetry.proto.logs.v1.logs_pb2 import LogsData
from sqlalchemy.orm import Session

from application_sdk.app.database import get_session
from application_sdk.app.rest.dto.telemetry import Log
from application_sdk.app.rest.interfaces.logs import Logs

router = APIRouter(
    prefix="/telemetry/v1/logs",
    tags=["logs"],
    responses={404: {"description": "Not found"}},
)


@router.get("", response_model=list[Log])
async def read_logs(
    req: Request,
    skip: int = 0,
    limit: int = 100,
    session: Session = Depends(get_session),
):
    """
    Retrieve a list of logs.

    :param skip: Number of logs to skip (for pagination).
    :param limit: Maximum number of logs to return.
    :param session: Database session.
    :param [attribute]__[operation]: Dynamically filter logs using query parameters. Filters are specified as
        `attribute__operation=value` where:
        - `attribute` is the field you want to filter (e.g., 'timestamp', 'severity', etc.).
        - `operation` is the filter operation (e.g., `eq`, `lt`, `gt`, `contains`, `ilike`, `like`).
        Supported operations:
        - eq: Equal to
        - ne: Not equal to
        - lt: Less than
        - gt: Greater than
        - contains: Substring containment
        - ilike: Case-insensitive LIKE
        - like: SQL LIKE
    :return: A list of Log objects.
    :raises HTTPException: If there's an error with the database operations.
    """
    try:
        query_filters = dict(req.query_params)
        del query_filters["skip"]
        del query_filters["limit"]

        return Logs.get_logs(
            session,
            skip,
            limit,
            query_filters=query_filters,
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


def get_logs_router() -> APIRouter:
    return router
