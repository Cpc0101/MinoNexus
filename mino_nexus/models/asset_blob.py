"""Content hash and reserved project capacity. The bytes live in the Scout library."""
from sqlalchemy import Column, String, Integer
from mino_nexus.core.database import Base


class AssetBlob(Base):
    __tablename__ = "asset_blobs"
    project_id = Column(String, primary_key=True)
    sha256 = Column(String, primary_key=True)
    md5 = Column(String, nullable=False)
    storage_id = Column(String, nullable=False)
    object_key = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    state = Column(String, nullable=False, default="pending")
    created_at = Column(Integer, nullable=False)
    verified_at = Column(Integer, nullable=False, default=0)
