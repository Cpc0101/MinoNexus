from sqlalchemy import Column, Integer, String
from mino_nexus.core.database import Base

class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id = Column(String, primary_key=True)
    user_id = Column(String, primary_key=True)
    added_by = Column(String, nullable=False)
    added_at_ms = Column(Integer, nullable=False)
