"""Account authentication snapshots; execution evidence stays on Scout."""
from sqlalchemy import Column, Integer, String, Text
from mino_nexus.core.database import Base


class WebAuthSnapshot(Base):
    __tablename__ = "web_auth_snapshots"

    scope_id = Column(String, primary_key=True)
    project_id = Column(String, nullable=False, index=True)
    app_id = Column(String, nullable=False, index=True)
    account_id = Column(String, nullable=False, index=True)
    env = Column(String, nullable=False)
    surface = Column(String, nullable=False)
    origin = Column(String, nullable=False)
    revision = Column(String, nullable=False)
    credential = Column(Text, nullable=False)
    created_at = Column(Integer, nullable=False)
    expires_at = Column(Integer, nullable=False)
    invalidated_at = Column(Integer, default=0, nullable=False)
