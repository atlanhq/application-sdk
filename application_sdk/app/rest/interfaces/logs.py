"""Interface for handling log-related API endpoints."""

import time
from datetime import UTC, datetime
from typing import List, Optional, Sequence

import pytz
from opentelemetry.proto.logs.v1.logs_pb2 import LogsData
from sqlalchemy.orm import Session

from application_sdk.app.models import Log


class Logs:
    @staticmethod
    def get_log(session: Session, log_id: int) -> Optional[Log]:
        """
        Get a log by ID.

        :param session: Database session.
        :param log_id: ID of the log to retrieve.
        :return: An Log object.
        """
        return session.query(Log).filter(Log.id == log_id).first()

    @staticmethod
    def get_logs(
        session: Session,
        skip: int = 0,
        limit: int = 100,
        keyword: str = "",
        from_timestamp: int = 0,
        to_timestamp: Optional[int] = None,
        client_tz: Optional[str] = None,
    ) -> Sequence[Log]:
        """
        Get logs with optional filtering by keyword and timestamp range.

        :param session: Database session.
        :param skip: Number of logs to skip (for pagination).
        :param limit: Maximum number of logs to return.
        :param keyword: Keyword to filter logs.
        :param from_timestamp: Start timestamp for log retrieval.
        :param to_timestamp: End timestamp for log retrieval.
        :param client_tz: IANA time zone name
        :return: A list of Log objects.
        """
        if to_timestamp is None:
            to_timestamp = int(time.time())
        logs = (
            session.query(Log)
            .filter(Log.body.contains(keyword))
            .filter(
                Log.timestamp >= datetime.fromtimestamp(from_timestamp, tz=UTC),
                Log.timestamp <= datetime.fromtimestamp(to_timestamp, tz=UTC),
            )
            .order_by(Log.timestamp.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

        if client_tz:
            for log in logs:
                log.timestamp = pytz.timezone("UTC").localize(log.timestamp)
                log.timestamp = log.timestamp.astimezone(pytz.timezone(client_tz))

                log.observed_timestamp = pytz.timezone("UTC").localize(
                    log.observed_timestamp
                )
                log.observed_timestamp = log.observed_timestamp.astimezone(
                    pytz.timezone(client_tz)
                )

        return logs

    @staticmethod
    def create_logs(session: Session, logs_data: LogsData) -> List[Log]:
        """
        Create logs from a protobuf message.

        :param session: Database session.
        :param logs_data: LogsData object containing log data.
        :return: A list of Log objects.
        """
        logs: List[Log] = []
        for resource_log in logs_data.resource_logs:
            resource_attributes = {}
            for resource_attribute in resource_log.resource.attributes:
                resource_attributes[resource_attribute.key] = (
                    resource_attribute.value.string_value
                )

            for scope_log in resource_log.scope_logs:
                for log in scope_log.log_records:
                    log_attributes = {}
                    for attribute in log.attributes:
                        log_attributes[attribute.key] = attribute.value.string_value

                    db_log = Log(
                        resource_attributes=resource_attributes,
                        scope_name=scope_log.scope.name,
                        severity=log.severity_text,
                        severity_number=log.severity_number.real,
                        observed_timestamp=datetime.fromtimestamp(
                            log.observed_time_unix_nano // 1000000000, tz=UTC
                        ),
                        timestamp=datetime.fromtimestamp(
                            log.observed_time_unix_nano // 1000000000, tz=UTC
                        ),
                        body=log.body.string_value,
                        trace_id=log.trace_id.hex(),
                        span_id=log.span_id.hex(),
                        attributes=log_attributes,
                    )
                    session.add(db_log)
                    logs.append(db_log)
        session.commit()

        for log in logs:
            session.refresh(log)
        return logs
