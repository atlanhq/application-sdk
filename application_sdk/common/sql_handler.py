import time
from sqlalchemy.engine import Engine
from sqlalchemy import event
from opentelemetry import metrics


class SQLHandler(object):
    meter = metrics.get_meter("sqlalchemy")

    gauge = meter.create_gauge(
        name="sqlalchemy_query_time.gauge",
        unit="seconds",
        description="Time taken to execute a query",
    )

    @staticmethod
    @event.listens_for(Engine, "before_cursor_execute")
    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault('query_start_time', []).append(time.time())

    @staticmethod
    @event.listens_for(Engine, "after_cursor_execute")
    def after_cursor_execute(conn,  cursor, statement, parameters, context, executemany):
        total = time.time() - conn.info['query_start_time'].pop(-1)
        trace_context = conn.info.get('trace_context', {})
        SQLHandler.gauge.set(total, {**trace_context, 'statement': statement})
