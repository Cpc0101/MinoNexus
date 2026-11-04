"""项目脚本仓库的应用清单。由 agent 通过 MCP 同步，Nexus 不读仓库、不碰 config.yaml。"""
from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, String

from mino_nexus.core.database import Base


class ProjectRepoApps(Base):
    __tablename__ = "project_repo_apps"

    project_id = Column(String, primary_key=True)
    revision = Column(String, default="")
    branch = Column(String, default="")
    apps = Column(JSON, default=dict)
    synced_by = Column(String, default="")
    synced_at = Column(Integer, default=0)
