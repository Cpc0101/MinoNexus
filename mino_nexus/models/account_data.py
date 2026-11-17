"""Lightweight account object memory and requested input descriptions."""
from sqlalchemy import Column, String, Text, Integer
from sqlalchemy.types import JSON
from mino_nexus.core.database import Base


class AccountData(Base):
    __tablename__ = "pool_account_data"
    project_id = Column(String, primary_key=True)
    account_id = Column(String, primary_key=True)
    data_id = Column(String, primary_key=True)
    app_id = Column(String, default="", index=True)
    kind = Column(String, nullable=False)
    object_ref = Column(String, nullable=False)
    title = Column(String, default="")
    attributes = Column(JSON, default=dict)
    source_task_id = Column(String, default="")
    updated_at = Column(Integer, nullable=False)


class AccountDataRequest(Base):
    __tablename__ = "pool_account_data_requests"
    project_id = Column(String, primary_key=True)
    account_id = Column(String, primary_key=True)
    run_id = Column(String, primary_key=True)
    session_id = Column(String, primary_key=True)
    app_id = Column(String, default="")
    case_id = Column(String, default="")
    data_need = Column(Text, nullable=False)
    updated_at = Column(Integer, nullable=False)
