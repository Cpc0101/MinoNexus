"""Agent execution state and ordered, append-only activity references."""
from sqlalchemy import Column, Integer, String, UniqueConstraint
from sqlalchemy.types import JSON
from mino_nexus.core.database import Base


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    owner_user_id = Column(String, primary_key=True)
    task_id = Column(String, primary_key=True)
    project_id = Column(String, nullable=False, default="", index=True)
    state = Column(JSON, nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    next_seq = Column(Integer, nullable=False, default=1)
    updated_at = Column(String, nullable=False, index=True)


class AgentTaskEvent(Base):
    __tablename__ = "agent_task_events"
    owner_user_id = Column(String, primary_key=True)
    task_id = Column(String, primary_key=True)
    seq = Column(Integer, primary_key=True)
    event_id = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(String, nullable=False)
    __table_args__ = (UniqueConstraint("owner_user_id", "task_id", "event_id"),)
