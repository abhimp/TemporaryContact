"""Retention for Temporary contacts + the two move operations and the side cache.

The Temporary address book is local: contacts get an expiry and are deleted when
it elapses. Google contacts live behind the live CardDAV proxy (see
google_proxy.py) — this module never syncs them. It only:
  - expires Temporary contacts,
  - moves a contact Temporary → Google (Keep) or Google → Temporary,
  - refreshes a side cache of Google contacts for the web UI to display.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from . import contacts as contacts_api
from .config import Config
from .db import (Database, DeletedContact, GoogleCacheEntry, RetentionRecord,
                 get_setting, set_setting)
from .google_link import GoogleApiError, GoogleNotConnected

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
    def __init__(self, cfg: Config, storage, db: Database, google_link=None):
        self.cfg = cfg
        self.storage = storage
        self.db = db
        self.google_link = google_link

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

    # ---- reconcile + expire (Temporary contacts only) ----

    def reconcile(self) -> None:
        cards = contacts_api.iter_all_contacts(self.storage)
        present = {(c["user"], c["addressbook"], c["href"]) for c in cards}
        default = self.default_seconds()
        now = _now()
        with self.db.session() as s:
            existing = {(r.user, r.addressbook, r.href): r
                        for r in s.query(RetentionRecord).all()}
            for c in cards:
                key = (c["user"], c["addressbook"], c["href"])
                rec = existing.get(key)
                if rec is None:
                    s.add(RetentionRecord(
                        user=c["user"], addressbook=c["addressbook"], href=c["href"],
                        uid=c["uid"], name=c["name"], first_seen=now,
                        expiry=now + timedelta(seconds=default),
                        retention_seconds=default))
                else:
                    rec.name = c["name"]
                    rec.uid = c["uid"] or rec.uid
            for key, rec in existing.items():
                if key not in present:
                    s.delete(rec)

    def expire(self) -> int:
        now = _now()
        with self.db.session() as s:
            # Compare in Python so SQLite's tz-naive storage can't skew the cutoff.
            targets = [(r.user, r.addressbook, r.href, r.name)
                       for r in s.query(RetentionRecord).all()
                       if _ensure_aware(r.expiry) <= now]
        deleted = 0
        for user, addressbook, href, name in targets:
            try:
                self._archive(user, addressbook, href, "expired", name)
                contacts_api.delete_contact(self.storage, user, addressbook, href)
                deleted += 1
            except Exception:
                log.exception("Failed to delete expired contact %s/%s/%s",
                              user, addressbook, href)
        if targets:
            with self.db.session() as s:
                for user, addressbook, href, _name in targets:
                    rec = (s.query(RetentionRecord)
                           .filter_by(user=user, addressbook=addressbook, href=href)
                           .first())
                    if rec:
                        s.delete(rec)
        return deleted

    def run_once(self) -> None:
        # Never touches Google — that stays live behind the proxy.
        self.reconcile()
        self.expire()
        self.purge_trash()

    # ---- Deleted archive (recoverable trash) ----

    def _archive(self, user: str, addressbook: str, href: str, reason: str,
                 fallback_name: str = "") -> None:
        vobj, _etag = contacts_api.get_contact(self.storage, user, addressbook, href)
        if vobj is None:
            return
        name = fallback_name
        try:
            if hasattr(vobj, "fn") and vobj.fn.value:
                name = vobj.fn.value
        except Exception:  # noqa: BLE001
            pass
        with self.db.session() as s:
            s.add(DeletedContact(user=user, name=name or "Contact",
                                 vcard=vobj.serialize(), reason=reason,
                                 deleted_at=_now()))

    def purge_trash(self) -> int:
        cutoff = _now() - timedelta(days=self.cfg.trash_retention_days)
        removed = 0
        with self.db.session() as s:
            for row in s.query(DeletedContact).all():
                if _ensure_aware(row.deleted_at) <= cutoff:
                    s.delete(row)
                    removed += 1
        return removed

    def list_trash(self, user: str) -> list[dict]:
        with self.db.session() as s:
            rows = (s.query(DeletedContact).filter_by(user=user)
                    .order_by(DeletedContact.deleted_at.desc()).all())
            return [{"id": r.id, "name": r.name or "Contact", "reason": r.reason,
                     "deleted_at": _ensure_aware(r.deleted_at)} for r in rows]

    def restore(self, user: str, trash_id: int) -> bool:
        with self.db.session() as s:
            row = s.get(DeletedContact, trash_id)
            if row is None or row.user != user:
                return False
            vcard = row.vcard
        contacts_api.create_local_contact(self.storage, user,
                                          contacts_api.DEFAULT_ADDRESSBOOK, vcard)
        with self.db.session() as s:
            row = s.get(DeletedContact, trash_id)
            if row:
                s.delete(row)
        return True

    def delete_forever(self, user: str, trash_id: int) -> bool:
        with self.db.session() as s:
            row = s.get(DeletedContact, trash_id)
            if row is None or row.user != user:
                return False
            s.delete(row)
            return True

    # ---- Temporary contact operations for the web UI ----

    def list_for_user(self, user: str) -> list[dict]:
        self.reconcile()
        cards = contacts_api.list_user_contacts(self.storage, user)
        now = _now()
        with self.db.session() as s:
            recs = {(r.addressbook, r.href): r
                    for r in s.query(RetentionRecord).filter_by(user=user).all()}
            result = []
            for c in cards:
                rec = recs.get((c["addressbook"], c["href"]))
                expiry = _ensure_aware(rec.expiry) if rec else None
                result.append({
                    **c,
                    "expiry": expiry,
                    "retention_seconds": rec.retention_seconds if rec else None,
                    "seconds_left": (expiry - now).total_seconds() if expiry else None,
                })
        result.sort(key=lambda c: (c["seconds_left"] is None, c["seconds_left"] or 0))
        return result

    def set_retention(self, user: str, addressbook: str, href: str,
                      seconds: float, mode: str = "set") -> None:
        """mode='set': expiry = now + duration. mode='extend': add duration to
        the current expiry (or now, if already past)."""
        now = _now()
        seconds = float(seconds)
        with self.db.session() as s:
            rec = (s.query(RetentionRecord)
                   .filter_by(user=user, addressbook=addressbook, href=href).first())
            if rec is None:
                rec = RetentionRecord(user=user, addressbook=addressbook, href=href,
                                      uid="", name="", first_seen=now)
                s.add(rec)
            current = _ensure_aware(rec.expiry)
            if mode == "extend" and current is not None:
                rec.expiry = max(current, now) + timedelta(seconds=seconds)
                if not rec.retention_seconds:
                    rec.retention_seconds = seconds
            else:
                rec.retention_seconds = seconds
                rec.expiry = now + timedelta(seconds=seconds)

    def _delete_local(self, user: str, addressbook: str, href: str) -> bool:
        """Remove a Temporary contact from storage + its record (no archiving)."""
        ok = contacts_api.delete_contact(self.storage, user, addressbook, href)
        with self.db.session() as s:
            rec = (s.query(RetentionRecord)
                   .filter_by(user=user, addressbook=addressbook, href=href).first())
            if rec:
                s.delete(rec)
        return ok

    def set_expiry(self, user: str, addressbook: str, href: str,
                   expiry_dt: datetime) -> None:
        """Set an absolute expiry datetime (from the 'Expire on' date picker)."""
        now = _now()
        with self.db.session() as s:
            rec = (s.query(RetentionRecord)
                   .filter_by(user=user, addressbook=addressbook, href=href).first())
            if rec is None:
                rec = RetentionRecord(user=user, addressbook=addressbook, href=href,
                                      uid="", name="", first_seen=now)
                s.add(rec)
            rec.expiry = expiry_dt
            rec.retention_seconds = max(0.0, (expiry_dt - now).total_seconds())

    def delete_now(self, user: str, addressbook: str, href: str) -> bool:
        # Manual delete → keep a recoverable copy in the archive.
        self._archive(user, addressbook, href, "deleted")
        return self._delete_local(user, addressbook, href)

    # ---- moves between the two books ----

    def keep_to_google(self, user: str, addressbook: str, href: str) -> None:
        """Keep: move a Temporary contact into Google, then drop the local copy."""
        if self.google_link is None or not self.google_link.enabled:
            raise GoogleNotConnected()
        vobj, _etag = contacts_api.get_contact(self.storage, user, addressbook, href)
        if vobj is None:
            raise LookupError("contact not found")
        uid = (getattr(getattr(vobj, "uid", None), "value", None)
               or href.rsplit(".", 1)[0])
        self.google_link.carddav_create(user, vobj.serialize(), uid)
        self._delete_local(user, addressbook, href)  # moved, not deleted — no archive

    def make_temporary(self, user: str, cache_id: int) -> bool:
        """Move a cached Google contact back to Temporary, deleting it in Google."""
        if self.google_link is None or not self.google_link.enabled:
            raise GoogleNotConnected()
        with self.db.session() as s:
            entry = s.get(GoogleCacheEntry, cache_id)
            if entry is None or entry.user != user:
                return False
            vcard, google_href = entry.vcard, entry.google_href
        # Create locally first (reliable), then remove from Google.
        contacts_api.create_local_contact(self.storage, user,
                                          contacts_api.DEFAULT_ADDRESSBOOK, vcard)
        self.google_link.carddav_delete(user, google_href)
        with self.db.session() as s:
            entry = s.get(GoogleCacheEntry, cache_id)
            if entry:
                s.delete(entry)
        return True

    # ---- side cache of Google contacts (WEB DISPLAY ONLY) ----

    def refresh_google_cache(self, user: str) -> int:
        if self.google_link is None or not self.google_link.enabled:
            raise GoogleNotConnected()
        entries = self.google_link.carddav_list(user)
        with self.db.session() as s:
            s.query(GoogleCacheEntry).filter_by(user=user).delete()
            for e in entries:
                s.add(GoogleCacheEntry(user=user, google_href=e["google_href"],
                                       name=e["name"], vcard=e["vcard"],
                                       etag=e["etag"]))
        return len(entries)

    def list_google_cache(self, user: str) -> list[dict]:
        with self.db.session() as s:
            rows = s.query(GoogleCacheEntry).filter_by(user=user).all()
        items = [{"id": r.id, "name": r.name or "Contact",
                  "google_href": r.google_href,
                  "search": contacts_api.vcard_search_text(r.name or "", r.vcard or "")}
                 for r in rows]
        items.sort(key=lambda c: c["name"].casefold())  # case-insensitive
        return items


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
