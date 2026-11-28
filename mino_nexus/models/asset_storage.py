"""Project quota for the publishing Scout's local library. Bytes are not stored here."""
from sqlalchemy import Column, String, Integer
from mino_nexus.core.database import Base


class AssetStorage(Base):
    __tablename__ = "asset_storages"
    project_id = Column(String, primary_key=True)
    storage_id = Column(String, primary_key=True)
    endpoint = Column(String, nullable=False)
    bucket = Column(String, nullable=False)
    region = Column(String, nullable=False)
    checksum_mode = Column(String, nullable=False)
    prefix = Column(String, nullable=False)
    access_key = Column(String, nullable=False)
    secret_key = Column(String, nullable=False)
    active = Column(Integer, nullable=False, default=0)
    quota_bytes = Column(Integer, nullable=False)
    max_preparations_per_task = Column(Integer, nullable=False, default=0)
    max_file_bytes = Column(Integer, nullable=False)
    updated_at = Column(Integer, nullable=False)
