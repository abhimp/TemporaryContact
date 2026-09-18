"""App metadata database (SQLite or PostgreSQL) via SQLAlchemy.

This stores retention/expiry bookkeeping and settings — NOT the contacts
themselves (those are vCard files owned by Radicale).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import logging

from sqlalchemy import (Boolean, DateTime, Float, String, Text,
                        UniqueConstraint, create_engine, inspect, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

log = logging.getLogger("temporarycontacts.db")

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
    # A "kept" contact was saved to Google and is permanent — never expires.
    kept: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("user", "addressbook", "href", name="uix_contact"),)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(512))


class DeletedContact(Base):
    """A recoverable archive of removed contacts (expired or manually deleted)."""
    __tablename__ = "deleted_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(512), default="")
    vcard: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(String(32), default="")  # "expired" | "deleted"
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class GoogleCredential(Base):
    """Per-user Google OAuth credentials (serialized authorized-user JSON)."""
    __tablename__ = "google_credentials"

    user: Mapped[str] = mapped_column(String(255), primary_key=True)
    token_json: Mapped[str] = mapped_column(String(4096))
    email: Mapped[str] = mapped_column(String(255), default="")


class GoogleCacheEntry(Base):
    """Side cache of a user's Google contacts, for WEB display only.

    Never read by the CardDAV sync path (that proxies live to Google). Populated
    on demand by the Refresh button.
    """
    __tablename__ = "google_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    user: Mapped[str] = mapped_column(String(255), index=True)
    google_href: Mapped[str] = mapped_column(String(512))
    name: Mapped[str] = mapped_column(String(512), default="")
    vcard: Mapped[str] = mapped_column(Text, default="")
    etag: Mapped[str] = mapped_column(String(512), default="")

    __table_args__ = (UniqueConstraint("user", "google_href", name="uix_cache"),)


class Database:
    def __init__(self, cfg: Config):
        url = cfg.sqlalchemy_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args,
                                    pool_pre_ping=True, future=True)
        Base.metadata.create_all(self.engine)
        self._migrate()
        self._Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def _migrate(self) -> None:
        """Add columns introduced after a table's first creation.

        create_all() makes new tables but won't alter existing ones, so upgrades
        that add a column to an existing DB need this.
        """
        insp = inspect(self.engine)
        # retention.kept (added for Google-linked/permanent contacts)
        if "retention" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("retention")}
            if "kept" not in cols:
                default = "false" if self.engine.dialect.name == "postgresql" else "0"
                try:
                    with self.engine.begin() as conn:
                        conn.execute(text(
                            f"ALTER TABLE retention ADD COLUMN kept BOOLEAN "
                            f"NOT NULL DEFAULT {default}"))
                    log.info("Migrated: added retention.kept column")
                except Exception:
                    log.exception("Failed adding retention.kept column")

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
