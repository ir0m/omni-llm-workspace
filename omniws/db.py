"""SQLite storage. Times are Unix milliseconds (UTC) so the browser can use them as they are."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, update
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class Base(DeclarativeBase):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


def make_engine(data_dir: Path) -> Engine:
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{data_dir / 'omni.db'}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def migrate(engine: Engine) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def mark_interrupted(sessions: sessionmaker[Session]) -> None:
    """Replies that were streaming when the server stopped can't continue."""
    from .models import Conversation, Message

    with sessions.begin() as session:
        session.execute(update(Message).where(Message.status == "streaming").values(status="interrupted"))
        for conversation in session.query(Conversation):
            if conversation.pending_inputs:
                conversation.pending_inputs = [{**item, "status": "returned"} for item in conversation.pending_inputs]


def session_dependency(sessions: sessionmaker[Session]):
    def get_session() -> Iterator[Session]:
        with sessions() as session:
            yield session

    return get_session
