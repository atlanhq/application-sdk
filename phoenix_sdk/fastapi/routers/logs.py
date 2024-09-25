from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from opentelemetry.proto.logs.v1.logs_pb2 import LogsData
from sqlalchemy.orm import Session

from phoenix_sdk.database import get_session
from phoenix_sdk.interfaces.logs import Logs
from phoenix_sdk.schemas import Log

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
    to_timestamp: Optional[int] = None,
    session: Session = Depends(get_session),
):
    return Logs.get_logs(session, skip, limit, keyword, from_timestamp, to_timestamp)


@router.post("", response_model=List[Log])
async def create_logs(request: Request, session: Session = Depends(get_session)):
    # Convert the request body to a protobuf message
    body = await request.body()
    log_message = LogsData()
    log_message.ParseFromString(body)
    return Logs.create_logs(session, log_message)
