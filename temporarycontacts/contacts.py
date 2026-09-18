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
    name = _display_name(item)
    search_parts = [name]
    try:
        vobj = item.vobject_item
        for key in ("tel", "email"):
            for comp in vobj.contents.get(key, []):
                if comp.value:
                    search_parts.append(str(comp.value))
    except Exception:  # noqa: BLE001
        pass
    return {
        "user": user,
        "addressbook": addressbook,
        "href": item.href,
        "uid": item.uid or "",
        "name": name,
        "search": " ".join(search_parts).lower(),
        "etag": item.etag,
    }


def vcard_search_text(name: str, vcard: str) -> str:
    """Lowercased name + phone/email values from a raw vCard, for searching."""
    parts = [name]
    for line in vcard.splitlines():
        upper = line.upper()
        if upper.startswith("TEL") or upper.startswith("EMAIL"):
            parts.append(line.split(":", 1)[-1])
    return " ".join(parts).lower()


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


def get_contact_vobject(storage, user: str, addressbook: str, href: str):
    """Return the vobject vCard for one contact, or None if it's gone."""
    path = f"/{user}/{addressbook}/"
    with storage.acquire_lock("r"):
        for item in storage.discover(path, "1"):
            if _is_collection(item):
                continue
            if item.href == href:
                return item.vobject_item
    return None


def get_contact(storage, user: str, addressbook: str, href: str):
    """Return (vobject_item, etag) for one contact, or (None, None)."""
    path = f"/{user}/{addressbook}/"
    with storage.acquire_lock("r"):
        for item in storage.discover(path, "1"):
            if _is_collection(item):
                continue
            if item.href == href:
                return item.vobject_item, item.etag
    return None, None


def create_local_contact(storage, user: str, addressbook: str, text: str) -> str:
    """Create a new contact in a local collection (creating it if needed).

    Returns the new item's href. Used by "Make Temporary" to recreate a Google
    contact in the local Temporary book.
    """
    import radicale.item as ritem

    path = f"/{user}/{addressbook}/"
    with storage.acquire_lock("w"):
        collections = [c for c in storage.discover(path, "0") if _is_collection(c)]
        if not collections:
            storage.create_collection(
                path, props={"tag": "VADDRESSBOOK", "D:displayname": "Temporary Contacts"})
            collections = [c for c in storage.discover(path, "0") if _is_collection(c)]
        collection = collections[0]
        item = ritem.Item(collection=collection, text=text)
        href = f"{item.uid or 'contact'}.vcf"
        collection.upload(href, item)
        return href


def save_contact(storage, user: str, addressbook: str, href: str, text: str):
    """Overwrite an existing contact's vCard. Returns the new etag, or None."""
    import radicale.item as ritem

    path = f"/{user}/{addressbook}/"
    with storage.acquire_lock("w"):
        collections = [c for c in storage.discover(path, "0") if _is_collection(c)]
        if not collections:
            return None
        collection = collections[0]
        item = ritem.Item(collection=collection, text=text)
        collection.upload(href, item)
        return item.etag


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
