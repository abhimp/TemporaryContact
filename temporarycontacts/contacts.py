"""Enumerate and delete contacts through Radicale's storage API.

Deleting via the storage API (rather than poking files) keeps the collection's
tag/sync state correct, so iOS notices removals on its next sync.
"""
from __future__ import annotations

from radicale.storage import BaseCollection


DEFAULT_ADDRESSBOOK = "addressbook"


def _is_collection(obj) -> bool:
    return isinstance(obj, BaseCollection)


def _clean(path: str) -> str:
    return path.strip("/")


def _display_name(item) -> str:
    try:
        vobj = item.vobject_item
        if hasattr(vobj, "fn") and vobj.fn.value:
            return str(vobj.fn.value)
        if hasattr(vobj, "n") and vobj.n.value:
            return str(vobj.n.value)
    except Exception:
        pass
    return item.uid or item.href


def _contact_dict(user: str, addressbook: str, item) -> dict:
    return {
        "user": user,
        "addressbook": addressbook,
        "href": item.href,
        "uid": item.uid or "",
        "name": _display_name(item),
    }


def _iter_addressbook(storage, user: str, home_path: str) -> list[dict]:
    out: list[dict] = []
    for ab in storage.discover("/" + _clean(home_path) + "/", "1"):
        if not _is_collection(ab) or ab.get_meta("tag") != "VADDRESSBOOK":
            continue
        ab_name = _clean(ab.path).split("/")[-1]
        for item in storage.discover("/" + _clean(ab.path) + "/", "1"):
            if _is_collection(item):
                continue
            out.append(_contact_dict(user, ab_name, item))
    return out


def list_user_contacts(storage, user: str) -> list[dict]:
    """All contacts belonging to one user."""
    with storage.acquire_lock("r"):
        return _iter_addressbook(storage, user, user)


def iter_all_contacts(storage) -> list[dict]:
    """All contacts across every user (for the background worker)."""
    out: list[dict] = []
    with storage.acquire_lock("r"):
        for home in storage.discover("/", "1"):
            if not _is_collection(home) or _clean(home.path) == "":
                continue
            user = _clean(home.path)
            out.extend(_iter_addressbook(storage, user, home.path))
    return out


def ensure_addressbook(storage, user: str,
                       displayname: str = "Temporary Contacts") -> bool:
    """Make sure the user has a default address book to sync into.

    iOS connects fine to an empty principal but has nothing to sync unless an
    address book collection exists. Radicale doesn't auto-create one, so we do.
    Returns True if it was created, False if it already existed.
    """
    path = f"/{user}/{DEFAULT_ADDRESSBOOK}/"
    with storage.acquire_lock("w"):
        for c in storage.discover(path, "0"):
            if _is_collection(c):
                return False
        storage.create_collection(
            path, props={"tag": "VADDRESSBOOK", "D:displayname": displayname})
        return True


def delete_contact(storage, user: str, addressbook: str, href: str) -> bool:
    """Delete a single contact. Returns True if it existed and was removed."""
    path = f"/{user}/{addressbook}/"
    with storage.acquire_lock("w"):
        collections = [c for c in storage.discover(path, "0") if _is_collection(c)]
        if not collections:
            return False
        collection = collections[0]
        # delete() raises if the item is missing; guard by checking hrefs.
        existing = {item.href for item in storage.discover(path, "1")
                    if not _is_collection(item)}
        if href not in existing:
            return False
        collection.delete(href)
        return True
