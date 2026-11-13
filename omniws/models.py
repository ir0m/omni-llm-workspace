from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, now_ms


class Conversation(Base):
    """A conversation in the app. Each turn may use a different provider, model or (later) agent."""

    __tablename__ = "conversations"

    pending_inputs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    mode: Mapped[str] = mapped_column(String(10), default="chat")  # chat | code
    project: Mapped[str | None] = mapped_column(String(200), default=None)
    # Temporary: kept here so it can be reopened, but agents run without saving sessions or memory
    temporary: Mapped[bool] = mapped_column(Boolean, default=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    # Larger comes first. New activity and manual reordering both set it.
    sort_key: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    forked_from: Mapped[str | None] = mapped_column(String(36), default=None)
    # The last selection, so the next message starts from it: {"provider", "model", "effort"}
    target: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Code mode: ask | auto_edit | auto (None: OMNI_APPROVAL_DEFAULT), and what "always allow" remembered
    approval_mode: Mapped[str | None] = mapped_column(String(12), default=None)
    allow_rules: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    updated_at: Mapped[int] = mapped_column(Integer, default=now_ms)

    messages: Mapped[list[Message]] = relationship(back_populates="conversation", order_by="Message.seq",
                                                   cascade="all, delete-orphan", passive_deletes=True)
    agent_sessions: Mapped[list[AgentSession]] = relationship(cascade="all, delete-orphan", passive_deletes=True)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("conversation_id", "seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(10))  # user | assistant
    # Content blocks (see blocks.py): text, thinking, tool_call, plan, handover
    content: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    provider: Mapped[str | None] = mapped_column(String(40), default=None)
    model: Mapped[str | None] = mapped_column(String(200), default=None)
    agent: Mapped[str | None] = mapped_column(String(40), default=None)
    mode: Mapped[str] = mapped_column(String(10), default="chat")
    # complete | streaming | error | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(12), default="complete")
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # The usage_update of the turn (camelCase, as in the event)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    # Imported history already known to this native session.
    native_origin: Mapped[dict[str, str] | None] = mapped_column(JSON, default=None)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AgentSession(Base):
    """An agent's own session (Claude Code session id, Codex thread id, ACP sessionId) tied to a conversation.
    `synced_seq` is the last message the agent has seen; later messages are handed over on its next turn."""

    __tablename__ = "agent_sessions"
    __table_args__ = (UniqueConstraint("conversation_id", "agent"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    agent: Mapped[str] = mapped_column(String(40))
    native_id: Mapped[str] = mapped_column(String(200))
    synced_seq: Mapped[int] = mapped_column(Integer, default=0)
    ephemeral: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)


class ExternalSession(Base):
    """An indexed CLI session. A null conversation is a tombstone after local deletion."""

    __tablename__ = "external_sessions"
    __table_args__ = (UniqueConstraint("family", "native_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family: Mapped[str] = mapped_column(String(40))
    agent: Mapped[str] = mapped_column(String(40))
    native_id: Mapped[str] = mapped_column(String(200))
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True)
    project: Mapped[str] = mapped_column(String(200))
    cwd: Mapped[str] = mapped_column(Text)
    turns: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pending: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class AppSetting(Base):
    """Small key/value settings changed in the app (within the limits `.env` sets)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text, default=None)


class Passkey(Base):
    """A WebAuthn credential (one per device). Sessions signed in with it end when it is deleted."""

    __tablename__ = "passkeys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    credential_id: Mapped[str] = mapped_column(String(400), unique=True)  # base64url
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    rp_id: Mapped[str] = mapped_column(String(253))
    name: Mapped[str] = mapped_column(String(100), default="")
    transports: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[int] = mapped_column(Integer, default=now_ms)
    last_used_at: Mapped[int | None] = mapped_column(Integer, default=None)


class EnrollToken(Base):
    """A one-time link to register a passkey without being signed in (the first device, or a lost one).
    Only the SHA-256 of the token is stored."""

    __tablename__ = "enroll_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[int] = mapped_column(Integer)
    used_at: Mapped[int | None] = mapped_column(Integer, default=None)
