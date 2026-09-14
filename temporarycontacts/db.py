"""App metadata database (SQLite or PostgreSQL) via SQLAlchemy.

This stores retention/expiry bookkeeping and settings — NOT the contacts
themselves (those are vCard files owned by Radicale).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import DateTime, Float, String, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import Config


class Base(DeclarativeBase):
    pass


class RetentionRecord(Base):
    __tablename__ = "retention"

    id: Mapped[int] = mapped_column(primary_key=True)
    user: Mapped[str] = mapped_column(String(255), index=True)
    addressbook: Mapped[str] = mapped_column(String(255))
    href: Mapped[str] = mapped_column(String(512))
    uid: Mapped[str] = mapped_column(String(512), default="")
    name: Mapped[str] = mapped_column(String(512), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    retention_seconds: Mapped[float] = mapped_column(Float)

    __table_args__ = (UniqueConstraint("user", "addressbook", "href", name="uix_contact"),)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(512))


class Database:
    def __init__(self, cfg: Config):
        url = cfg.sqlalchemy_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args,
                                    pool_pre_ping=True, future=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @contextmanager
    def session(self):
        s = self._Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


def get_setting(session, key: str) -> str | None:
    row = session.get(Setting, key)
    return row.value if row else None


def set_setting(session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
