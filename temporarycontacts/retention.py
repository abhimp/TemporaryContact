"""Retention logic: assign expiry to new contacts, delete expired ones."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from . import contacts as contacts_api
from .config import Config
from .db import (Database, GoogleContactLink, RetentionRecord, get_setting,
                 set_setting)
from .google_link import GoogleApiError, GoogleContactGone, GoogleNotConnected

log = logging.getLogger("temporarycontacts.retention")

DEFAULT_SETTING_KEY = "default_retention_seconds"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """Treat DB-returned datetimes as UTC (SQLite drops tzinfo)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class RetentionService:
    """Shared by the web UI (on-demand) and the background worker (periodic)."""

    def __init__(self, cfg: Config, storage, db: Database, google_link=None):
        self.cfg = cfg
        self.storage = storage
        self.db = db
        self.google_link = google_link

    def _links_for(self, session, user: str | None = None) -> dict:
        q = session.query(GoogleContactLink)
        if user is not None:
            q = q.filter_by(user=user)
        return {(l.user, l.addressbook, l.href): l for l in q.all()}

    # ---- default retention (config default, overridable via Settings) ----

    def default_seconds(self) -> float:
        with self.db.session() as s:
            val = get_setting(s, DEFAULT_SETTING_KEY)
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        return self.cfg.default_retention_seconds

    def set_default_seconds(self, seconds: float) -> None:
        with self.db.session() as s:
            set_setting(s, DEFAULT_SETTING_KEY, str(float(seconds)))

    # ---- reconcile: ensure a record exists for every contact; prune stale ----

    def reconcile(self) -> None:
        cards = contacts_api.iter_all_contacts(self.storage)
        present = {(c["user"], c["addressbook"], c["href"]) for c in cards}
        default = self.default_seconds()
        now = _now()
        with self.db.session() as s:
            existing = {(r.user, r.addressbook, r.href): r
                        for r in s.query(RetentionRecord).all()}
            links = self._links_for(s)
            for c in cards:
                key = (c["user"], c["addressbook"], c["href"])
                is_kept = key in links
                rec = existing.get(key)
                if rec is None:
                    s.add(RetentionRecord(
                        user=c["user"], addressbook=c["addressbook"], href=c["href"],
                        uid=c["uid"], name=c["name"], first_seen=now,
                        expiry=now + timedelta(seconds=default),
                        retention_seconds=default, kept=is_kept,
                    ))
                else:
                    rec.name = c["name"]
                    rec.uid = c["uid"] or rec.uid
                    rec.kept = is_kept
            for key, rec in existing.items():
                if key not in present:
                    s.delete(rec)
            # Prune links whose contact no longer exists (Google copy is left intact).
            for key, link in links.items():
                if key not in present:
                    s.delete(link)

    # ---- expire: delete contacts whose retention has elapsed ----

    def expire(self) -> int:
        now = _now()
        with self.db.session() as s:
            # Compare in Python so SQLite's tz-naive storage can't skew the cutoff.
            # Kept (saved-to-Google) contacts are permanent and never expire.
            targets = [(r.user, r.addressbook, r.href)
                       for r in s.query(RetentionRecord).all()
                       if not r.kept and _ensure_aware(r.expiry) <= now]

        deleted = 0
        for user, addressbook, href in targets:
            try:
                contacts_api.delete_contact(self.storage, user, addressbook, href)
                deleted += 1
            except Exception:
                log.exception("Failed to delete expired contact %s/%s/%s",
                              user, addressbook, href)

        if targets:
            with self.db.session() as s:
                for user, addressbook, href in targets:
                    rec = (s.query(RetentionRecord)
                           .filter_by(user=user, addressbook=addressbook, href=href)
                           .first())
                    if rec:
                        s.delete(rec)
        return deleted

    def run_once(self) -> None:
        self.reconcile()
        self.push_google_updates()
        self.expire()

    # ---- Google linking + ongoing push ----

    def link_contact(self, user: str, addressbook: str, href: str) -> None:
        """Save a contact to Google and keep it linked (permanent, auto-pushed)."""
        if self.google_link is None or not self.google_link.enabled:
            raise GoogleNotConnected()
        vobj, etag = contacts_api.get_contact(self.storage, user, addressbook, href)
        if vobj is None:
            raise LookupError("contact not found")
        resource_name = self.google_link.create_contact(user, vobj)
        now = _now()
        with self.db.session() as s:
            link = (s.query(GoogleContactLink)
                    .filter_by(user=user, addressbook=addressbook, href=href).first())
            if link is None:
                link = GoogleContactLink(user=user, addressbook=addressbook, href=href)
                s.add(link)
            link.resource_name = resource_name
            link.source_etag = etag or ""
            rec = (s.query(RetentionRecord)
                   .filter_by(user=user, addressbook=addressbook, href=href).first())
            if rec is None:
                rec = RetentionRecord(user=user, addressbook=addressbook, href=href,
                                      uid="", name="", first_seen=now,
                                      expiry=now, retention_seconds=0)
                s.add(rec)
            rec.kept = True

    def push_google_updates(self) -> int:
        """Push changed linked contacts to Google. Returns count pushed."""
        if self.google_link is None or not self.google_link.enabled:
            return 0
        with self.db.session() as s:
            links = [(l.user, l.addressbook, l.href, l.resource_name, l.source_etag)
                     for l in s.query(GoogleContactLink).all()]
        pushed = 0
        for user, addressbook, href, resource_name, source_etag in links:
            vobj, etag = contacts_api.get_contact(self.storage, user, addressbook, href)
            if vobj is None or etag == source_etag:
                continue  # gone (reconcile prunes it) or unchanged
            try:
                self.google_link.update_contact(user, resource_name, vobj)
            except GoogleContactGone:
                # Google copy was deleted: drop the link so it resumes normal retention.
                self._drop_link(user, addressbook, href, unkeep=True)
                continue
            except (GoogleApiError, GoogleNotConnected):
                log.exception("Failed pushing update to Google for %s/%s/%s",
                              user, addressbook, href)
                continue
            with self.db.session() as s:
                link = (s.query(GoogleContactLink)
                        .filter_by(user=user, addressbook=addressbook, href=href).first())
                if link:
                    link.source_etag = etag
            pushed += 1
        return pushed

    def _drop_link(self, user: str, addressbook: str, href: str,
                   unkeep: bool = False) -> None:
        with self.db.session() as s:
            link = (s.query(GoogleContactLink)
                    .filter_by(user=user, addressbook=addressbook, href=href).first())
            if link:
                s.delete(link)
            if unkeep:
                rec = (s.query(RetentionRecord)
                       .filter_by(user=user, addressbook=addressbook, href=href).first())
                if rec:
                    rec.kept = False

    # ---- per-contact operations for the web UI ----

    def list_for_user(self, user: str) -> list[dict]:
        self.reconcile()
        cards = contacts_api.list_user_contacts(self.storage, user)
        now = _now()
        with self.db.session() as s:
            recs = {(r.addressbook, r.href): r
                    for r in s.query(RetentionRecord).filter_by(user=user).all()}
            links = self._links_for(s, user)
            result = []
            for c in cards:
                rec = recs.get((c["addressbook"], c["href"]))
                linked = (user, c["addressbook"], c["href"]) in links
                expiry = _ensure_aware(rec.expiry) if rec else None
                kept = bool(rec.kept) if rec else linked
                result.append({
                    **c,
                    "expiry": None if kept else expiry,
                    "retention_seconds": rec.retention_seconds if rec else None,
                    "seconds_left": None if kept else (
                        (expiry - now).total_seconds() if expiry else None),
                    "kept": kept,
                })
        # Kept contacts last; otherwise soonest-to-expire first.
        result.sort(key=lambda c: (c["kept"], c["seconds_left"] is None,
                                   c["seconds_left"] or 0))
        return result

    def set_retention(self, user: str, addressbook: str, href: str, seconds: float) -> None:
        now = _now()
        with self.db.session() as s:
            rec = (s.query(RetentionRecord)
                   .filter_by(user=user, addressbook=addressbook, href=href).first())
            if rec is None:
                rec = RetentionRecord(user=user, addressbook=addressbook, href=href,
                                      uid="", name="", first_seen=now)
                s.add(rec)
            rec.retention_seconds = float(seconds)
            rec.expiry = now + timedelta(seconds=float(seconds))

    def delete_now(self, user: str, addressbook: str, href: str) -> bool:
        ok = contacts_api.delete_contact(self.storage, user, addressbook, href)
        with self.db.session() as s:
            rec = (s.query(RetentionRecord)
                   .filter_by(user=user, addressbook=addressbook, href=href).first())
            if rec:
                s.delete(rec)
            link = (s.query(GoogleContactLink)
                    .filter_by(user=user, addressbook=addressbook, href=href).first())
            if link:
                s.delete(link)  # remove the link; the Google copy is left intact
        return ok

    def push_after_edit(self, user: str, addressbook: str, href: str) -> None:
        """Immediately push a just-edited contact to Google if it's linked."""
        if self.google_link is None or not self.google_link.enabled:
            return
        with self.db.session() as s:
            link = (s.query(GoogleContactLink)
                    .filter_by(user=user, addressbook=addressbook, href=href).first())
            resource_name = link.resource_name if link else None
        if not resource_name:
            return
        vobj, etag = contacts_api.get_contact(self.storage, user, addressbook, href)
        if vobj is None:
            return
        try:
            self.google_link.update_contact(user, resource_name, vobj)
        except GoogleContactGone:
            self._drop_link(user, addressbook, href, unkeep=True)
            return
        except (GoogleApiError, GoogleNotConnected):
            log.exception("Failed pushing edit to Google for %s/%s/%s",
                          user, addressbook, href)
            return
        with self.db.session() as s:
            link = (s.query(GoogleContactLink)
                    .filter_by(user=user, addressbook=addressbook, href=href).first())
            if link:
                link.source_etag = etag or ""


class RetentionWorker:
    """Runs RetentionService.run_once() on an interval in a daemon thread."""

    def __init__(self, service: RetentionService):
        self.service = service
        self.interval = max(30, int(service.cfg.scan_interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="retention", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while True:
            try:
                self.service.run_once()
            except Exception:
                log.exception("Retention pass failed")
            if self._stop.wait(self.interval):
                break
