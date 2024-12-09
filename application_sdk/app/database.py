import json
import os
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker


@lru_cache(maxsize=None)
def get_engine():
    """Get the database engine."""
    sqlalchemy_database_url = (
        os.getenv("ATLAN_SQLALCHEMY_DATABASE_URL") or "sqlite:////tmp/app.db"
    )
    sqlalchemy_connect_args = (
        os.getenv("ATLAN_SQLALCHEMY_CONNECT_ARGS") or """{"check_same_thread": false}"""
    )

    return create_engine(
        sqlalchemy_database_url,
        connect_args=json.loads(sqlalchemy_connect_args),
        pool_pre_ping=True,
    )


def get_session():
    """Get the database session."""
    session = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())()
    try:
        yield session
    finally:
        session.close()


Base = declarative_base()
