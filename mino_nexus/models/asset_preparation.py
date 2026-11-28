"""Shared resource preparation claims; never execute or generate on Nexus."""
from sqlalchemy import Column, String, Integer
from sqlalchemy.types import JSON
from mino_nexus.core.database import Base


class AssetPreparation(Base):
    __tablename__ = "asset_preparations"
    project_id = Column(String, primary_key=True)
    preparation_id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False)
    owner_user_id = Column(String, nullable=False)
    attempts = Column(JSON, nullable=False, default=list)
    demand = Column(JSON, nullable=False)
    recipe_ref = Column(String, nullable=False)
    state = Column(String, nullable=False)
    asset_ref = Column(JSON, nullable=False, default=dict)
    evidence = Column(JSON, nullable=False, default=dict)
    updated_at = Column(Integer, nullable=False)
