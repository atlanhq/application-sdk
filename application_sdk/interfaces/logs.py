import time
from datetime import UTC, datetime
from typing import List, Optional, Sequence

from opentelemetry.proto.logs.v1.logs_pb2 import LogsData
from sqlalchemy.orm import Session

from application_sdk.models import Log


class Logs:
    @staticmethod
    def get_log(session: Session, event_id: int) -> Optional[Log]:
        return session.query(Log).filter(Log.id == event_id).first()

    @staticmethod
    def get_logs(
        session: Session,
        skip: int = 0,
        limit: int = 100,
        keyword: str = "",
        from_timestamp: int = 0,
        to_timestamp: Optional[int] = None,
    ) -> Sequence[Log]:
        if to_timestamp is None:
            to_timestamp = int(time.time())
        return (
            session.query(Log)
            .filter(Log.body.contains(keyword))
            .filter(
                Log.timestamp >= datetime.fromtimestamp(from_timestamp),
                Log.timestamp <= datetime.fromtimestamp(to_timestamp),
            )
            .order_by(Log.timestamp.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    @staticmethod
    def create_logs(session: Session, logs_data: LogsData) -> List[Log]:
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
