"""MCP OAuth 2.1：动态客户端、一次性授权码、轮换的访问令牌和刷新令牌。

只存哈希。明文只在签发响应里出现一次。
"""
from sqlalchemy import Boolean, Column, Integer, String, Text

from mino_nexus.core.database import Base


class OauthClient(Base):
    __tablename__ = "oauth_clients"

    client_id = Column(String, primary_key=True)
    client_name = Column(String, nullable=False, default="")
    redirect_uris = Column(Text, nullable=False, default="[]")
    auth_method = Column(String, nullable=False, default="none")
    secret_hash = Column(String, nullable=False, default="")
    source = Column(String, nullable=False, default="dcr")
    created_at = Column(Integer, nullable=False, default=0)


class OauthRequest(Base):
    """浏览器同意之前的授权事务。txn 本身是密钥。"""

    __tablename__ = "oauth_requests"

    txn = Column(String, primary_key=True)
    client_id = Column(String, nullable=False, index=True)
    client_name = Column(String, nullable=False, default="")
    redirect_uri = Column(Text, nullable=False, default="")
    code_challenge = Column(String, nullable=False, default="")
    resource = Column(Text, nullable=False, default="")
    scope = Column(String, nullable=False, default="")
    state = Column(Text, nullable=False, default="")
    expires_at = Column(Integer, nullable=False, default=0)
    created_at = Column(Integer, nullable=False, default=0)


class OauthCode(Base):
    __tablename__ = "oauth_codes"

    code_hash = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    client_id = Column(String, nullable=False)
    client_name = Column(String, nullable=False, default="")
    redirect_uri = Column(Text, nullable=False, default="")
    code_challenge = Column(String, nullable=False, default="")
    resource = Column(Text, nullable=False, default="")
    scope = Column(String, nullable=False, default="")
    state = Column(Text, nullable=False, default="")
    expires_at = Column(Integer, nullable=False, default=0)
    used = Column(Boolean, nullable=False, default=False)


class OauthToken(Base):
    __tablename__ = "oauth_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    family_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    client_id = Column(String, nullable=False, default="")
    client_name = Column(String, nullable=False, default="")
    kind = Column(String, nullable=False, default="")
    token_hash = Column(String, nullable=False, unique=True, index=True)
    prefix = Column(String, nullable=False, default="")
    resource = Column(Text, nullable=False, default="")
    scope = Column(String, nullable=False, default="")
    expires_at = Column(Integer, nullable=False, default=0)
    family_created = Column(Integer, nullable=False, default=0)
    replaced = Column(Boolean, nullable=False, default=False)
    revoked = Column(Boolean, nullable=False, default=False)
    created_at = Column(Integer, nullable=False, default=0)
    last_used_at = Column(Integer, nullable=False, default=0)
