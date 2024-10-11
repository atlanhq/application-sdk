"""Interface for handling log-related API endpoints."""

from datetime import UTC, datetime
from typing import Dict, List, Optional, Sequence

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
        query_dict: Dict[str, str] = {},
    ) -> Sequence[Log]:
        """
        Get logs with optional filtering by keyword and timestamp range.

        :param session: Database session.
        :param skip: Number of logs to skip (for pagination).
        :param limit: Maximum number of logs to return.
        :param keyword: Keyword to filter logs.
        :param from_timestamp: Start timestamp for log retrieval.
        :param to_timestamp: End timestamp for log retrieval.
        :return: A list of Log objects.
        """
        output = session.query(Log)

        for key in query_dict:
            path = key.split("__")[0]
            log_attribute = path.split(".")[0]
            log_key = ".".join(path.split(".")[1:])

            operation = key.split("__")[1]

            # Conversion to method names in SQLAlchemy
            # https://docs.sqlalchemy.org/en/20/core/operators.html#comparison-operators
            if operation in ["eq", "ne", "lt", "gt"]:
                operation = "__" + operation + "__"

            value = query_dict[key]
            column = getattr(Log, log_attribute)

            if str(column.type) == "DATETIME":
                value = datetime.fromtimestamp(int(value), tz=UTC)
            
            if log_key:
                output = output.filter(getattr(column[log_key], operation)(value))
            else:
                output = output.filter(getattr(column, operation)(value))

        return output.order_by(Log.timestamp.desc()).offset(skip).limit(limit).all()

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
