"""Durable dispatch and result index; execution evidence remains on the Scout."""
from sqlalchemy import Boolean, Column, Integer, JSON, String

from mino_nexus.core.database import Base


class ScoutRecord(Base):
    __tablename__ = "scout_record_index"

    session_id = Column(String, primary_key=True)
    attempt_id = Column(String, nullable=False, unique=True)
    task_id = Column(String, nullable=False, index=True)
    owner_user_id = Column(String, nullable=False, index=True)
    node_id = Column(String, nullable=False, index=True)
    case_id = Column(String, nullable=False)
    sn = Column(String, nullable=False)
    status = Column(String, nullable=False, default="queued", index=True)
    summary_version = Column(Integer, nullable=False, default=0)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    payload = Column(JSON, nullable=False, default=dict)
    context = Column(JSON, nullable=False, default=dict)
    summary = Column(JSON, nullable=False, default=dict)
    updated_at = Column(String, nullable=False)
