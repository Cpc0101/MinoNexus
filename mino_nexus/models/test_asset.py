"""Immutable logical asset versions; no binary contents or temporary URLs."""
from sqlalchemy import Column, String, Integer
from sqlalchemy.types import JSON
from mino_nexus.core.database import Base


class TestAsset(Base):
    __tablename__ = "test_assets"
    project_id = Column(String, primary_key=True)
    asset_id = Column(String, primary_key=True)
    version = Column(Integer, primary_key=True)
    sha256 = Column(String, nullable=False)
    title = Column(String, nullable=False)
    filename = Column(String, nullable=False)
    kind = Column(String, nullable=False, index=True)
    attributes = Column(JSON, nullable=False, default=dict)
    source = Column(JSON, nullable=False, default=dict)
    created_by = Column(String, nullable=False)
    created_at = Column(Integer, nullable=False)
