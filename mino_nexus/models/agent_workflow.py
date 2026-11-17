"""User-owned Agent progress, independent of Scout execution verdicts."""
from sqlalchemy import Column, Integer, String
from sqlalchemy.types import JSON
from mino_nexus.core.database import Base


class AgentWorkflow(Base):
    __tablename__ = "agent_workflows"
    owner_user_id = Column(String, primary_key=True)
    workflow_id = Column(String, primary_key=True)
    state = Column(JSON, nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    updated_at = Column(String, nullable=False)
