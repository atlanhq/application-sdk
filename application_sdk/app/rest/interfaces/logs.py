"""Interface for handling log-related API endpoints."""

import time
from datetime import UTC, datetime
from typing import List, Optional, Sequence

import pytz
from opentelemetry.proto.logs.v1.logs_pb2 import LogsData
from sqlalchemy import func
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

        client_timezone = UTC
        if client_tz:
            client_timezone = pytz.timezone(client_tz)
            tz_offset = (
                client_timezone.utcoffset(datetime.utcnow()).total_seconds() / 3600.0
            )
        else:
            tz_offset = 0  # Default to UTC if no client timezone is provided

        query_response = (
            session.query(
                Log,
            )
            .add_columns(
                # Convert timestamp and observed_timestamp to client's timezone
                func.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    func.datetime(Log.timestamp, f"{tz_offset} hours"),
                ).label("timestamp"),
                func.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    func.datetime(Log.observed_timestamp, f"{tz_offset} hours"),
                ).label("observed_timestamp"),
            )
            .filter(Log.body.contains(keyword))
            .filter(
                func.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    func.datetime(Log.timestamp, f"{tz_offset} hours"),
                ).label("timestamp")
                >= datetime.fromtimestamp(from_timestamp, tz=client_timezone),
                func.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    func.datetime(Log.timestamp, f"{tz_offset} hours"),
                ).label("timestamp")
                <= datetime.fromtimestamp(to_timestamp, tz=client_timezone),
            )
            .order_by(Log.timestamp.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        logs = []

        for row in query_response:
            # row is a tuple of Log, timestamp, and observed_timestamp
            row[0].timestamp = row[1]
            row[0].observed_timestamp = row[2]
            logs.append(row[0])

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
