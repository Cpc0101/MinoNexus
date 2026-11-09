"""LangBot 入站收件箱：给 MCP agent 拉取消息用的路由元数据。

正文不进本表 —— 渠道消息真源在 Scout 本地库，需要正文时经 `plugin.im.history` 现取。
这里只保留「回复这条消息需要什么」：渠道、实例、会话、发送者、回复上下文与接管状态。
只收录 LangBot 渠道；微信 iLink 不在此范围。
"""
from __future__ import annotations

from sqlalchemy import JSON, Column, Index, Integer, String

from mino_nexus.core.database import Base


class ImInboxItem(Base):
    """一条已确认归属的 LangBot 入站消息（不含正文）。"""

    __tablename__ = "im_inbox_items"

    # 与入站去重 key 同源：重复投递天然幂等，不需要额外唯一约束。
    id = Column(String, primary_key=True)
    user_id = Column(String, index=True, nullable=False)
    node_id = Column(String, default="", nullable=False)
    channel = Column(String, index=True, default="", nullable=False)  # langbot:feishu / langbot:wecom / langbot:dingtalk
    instance_id = Column(String, default="", nullable=False)
    tenant = Column(String, default="", nullable=False)
    chat_id = Column(String, default="", nullable=False)
    chat_type = Column(String, default="private", nullable=False)
    sender_id = Column(String, default="", nullable=False)
    msg_id = Column(String, default="", nullable=False)
    event_type = Column(String, default="message", nullable=False)
    reply_ctx = Column(JSON, default=dict)
    status = Column(String, index=True, default="pending", nullable=False)  # pending | handled
    handled_by = Column(String, default="", nullable=False)  # mino（内建助手）| mcp（外部 agent）
    handled_at = Column(Integer, default=0, nullable=False)
    created_at = Column(Integer, index=True, default=0, nullable=False)

    __table_args__ = (Index("ix_im_inbox_user_status", "user_id", "status", "created_at"),)
