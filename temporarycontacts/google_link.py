"""Google integration: OAuth + "Keep / Save to Google" via the People API.

Per-user OAuth. When a user clicks Keep, the contact's vCard is converted to a
Google People `Person` and created in their Google Contacts; the caller then
removes it from the Temporary account.
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote
from xml.etree import ElementTree as ET

# Google may return granted scopes in a different order/set than requested
# (e.g. adding openid); without this, requests-oauthlib raises "Scope has changed".
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .config import Config
from .db import Database, GoogleCredential

log = logging.getLogger("temporarycontacts.google")

SCOPES = ["https://www.googleapis.com/auth/carddav",
          "https://www.googleapis.com/auth/contacts",
          "https://www.googleapis.com/auth/userinfo.email", "openid"]

# Google's CardDAV service (what iOS itself talks to for Google accounts).
GOOGLE_HOST = "https://www.googleapis.com"
CARDDAV_ROOT = GOOGLE_HOST + "/carddav/v1"
CARDDAV_PRINCIPAL = CARDDAV_ROOT + "/principals/{email}/lists/default/"
USERINFO_URL = GOOGLE_HOST + "/oauth2/v3/userinfo"


class GoogleLink:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.google_enabled and self.cfg.google_client_id
                    and self.cfg.google_client_secret)

    def _client_config(self) -> dict:
        return {"web": {
            "client_id": self.cfg.google_client_id,
            "client_secret": self.cfg.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [self.cfg.google_callback_url],
        }}

    def _flow(self, state: str | None = None) -> Flow:
        return Flow.from_client_config(
            self._client_config(), scopes=SCOPES,
            redirect_uri=self.cfg.google_callback_url, state=state)

    # ---- OAuth flow ----

    def authorization_url(self) -> tuple[str, str, str]:
        """Return (auth_url, state, code_verifier). The verifier (PKCE) must be
        persisted and handed back to finish_authorization on the callback."""
        flow = self._flow()
        url, state = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent")
        return url, state, flow.code_verifier

    def finish_authorization(self, user: str, query_string: str, state: str,
                             code_verifier: str | None = None) -> str:
        flow = self._flow(state=state)
        # PKCE: the verifier generated at authorization time must be replayed here.
        flow.code_verifier = code_verifier
        # Rebuild the callback URL from the configured (https) public URL. Behind a
        # TLS-terminating reverse proxy the request reaches us as http, which the
        # OAuth library rejects as "insecure_transport"; the public URL is https.
        response_url = self.cfg.google_callback_url
        if query_string:
            response_url += "?" + query_string
        flow.fetch_token(authorization_response=response_url)
        creds = flow.credentials
        email = self._fetch_email(creds)
        self._store(user, creds, email)
        return email

    # ---- credential storage ----

    def _store(self, user: str, creds: Credentials, email: str = "") -> None:
        with self.db.session() as s:
            row = s.get(GoogleCredential, user)
            if row is None:
                row = GoogleCredential(user=user)
                s.add(row)
            row.token_json = creds.to_json()
            if email:
                row.email = email

    def connection(self, user: str) -> dict | None:
        with self.db.session() as s:
            row = s.get(GoogleCredential, user)
            return {"email": row.email} if row else None

    def disconnect(self, user: str) -> None:
        with self.db.session() as s:
            row = s.get(GoogleCredential, user)
            if row:
                s.delete(row)

    def _load_credentials(self, user: str) -> Credentials | None:
        with self.db.session() as s:
            row = s.get(GoogleCredential, user)
            if not row:
                return None
            token = row.token_json
        # Do NOT override scopes here — a token refreshes with the scopes it was
        # actually granted. Forcing the current SCOPES list (e.g. after adding
        # carddav) makes refresh request un-granted scopes → invalid_scope.
        return Credentials.from_authorized_user_info(json.loads(token))

    def _fetch_email(self, creds: Credentials) -> str:
        try:
            r = AuthorizedSession(creds).get(USERINFO_URL, timeout=15)
            if r.ok:
                return r.json().get("email", "")
        except Exception:
            log.exception("Could not fetch Google account email")
        return ""

    # ---- diagnostics ----

    def carddav_probe(self, user: str) -> dict:
        """PROPFIND Google's CardDAV endpoint to confirm OAuth + scope work."""
        creds = self._load_credentials(user)
        if creds is None:
            return {"ok": False, "error": "user not connected to Google"}
        session = AuthorizedSession(creds)
        email = self._fetch_email(creds) or ""
        url = CARDDAV_PRINCIPAL.format(email=email)
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<propfind xmlns="DAV:"><prop><displayname/><resourcetype/>'
                '</prop></propfind>')
        try:
            resp = session.request(
                "PROPFIND", url,
                headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                data=body, timeout=30)
        except Exception as exc:  # noqa: BLE001
            self._store(user, creds)
            return {"ok": False, "url": url, "error": str(exc)}
        self._store(user, creds)
        hint = ""
        if resp.status_code == 403:
            hint = "403 — the OAuth token likely lacks the carddav scope; reconnect Google."
        elif resp.status_code == 401:
            hint = "401 — token invalid/expired; reconnect Google."
        return {"ok": resp.ok, "status": resp.status_code, "email": email,
                "url": url, "hint": hint, "body": resp.text[:400]}

    # ---- Google CardDAV reads/writes (web cache + move buttons) ----
    #
    # These are used ONLY by the web UI (cache display, Keep, Make Temporary).
    # The live iPhone sync path is the reverse-proxy in google_proxy.py and does
    # not go through here.

    def _session_email(self, user: str):
        creds = self._load_credentials(user)
        conn = self.connection(user)
        if creds is None or not conn or not conn.get("email"):
            return None, None, None
        return AuthorizedSession(creds), conn["email"], creds

    def _list_url(self, email: str) -> str:
        return CARDDAV_PRINCIPAL.format(email=quote(email))

    def carddav_list(self, user: str) -> list[dict]:
        """All Google contacts as [{google_href, etag, vcard, name}]."""
        session, email, creds = self._session_email(user)
        if session is None:
            raise GoogleNotConnected()
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<C:addressbook-query xmlns:D="DAV:" '
                'xmlns:C="urn:ietf:params:xml:ns:carddav">'
                '<D:prop><D:getetag/><C:address-data/></D:prop>'
                '<C:filter/></C:addressbook-query>')
        try:
            resp = session.request(
                "REPORT", self._list_url(email),
                headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                data=body, timeout=60)
        finally:
            self._store(user, creds)
        if resp.status_code != 207:
            raise GoogleApiError(f"list {resp.status_code}: {resp.text[:300]}")
        return _parse_addressbook(resp.content)

    def carddav_create(self, user: str, vcard_text: str, uid: str) -> str:
        """PUT a new vCard into Google; returns the Google href."""
        session, email, creds = self._session_email(user)
        if session is None:
            raise GoogleNotConnected()
        href = self._list_url(email) + quote(uid) + ".vcf"
        try:
            resp = session.put(
                href,
                headers={"Content-Type": "text/vcard; charset=utf-8",
                         "If-None-Match": "*"},
                data=vcard_text.encode("utf-8"), timeout=30)
        finally:
            self._store(user, creds)
        if resp.status_code not in (200, 201, 204):
            raise GoogleApiError(f"create {resp.status_code}: {resp.text[:300]}")
        return href

    def carddav_delete(self, user: str, google_href: str) -> bool:
        """DELETE a Google contact by href (path or full URL)."""
        session, email, creds = self._session_email(user)
        if session is None:
            raise GoogleNotConnected()
        url = google_href if google_href.startswith("http") else GOOGLE_HOST + google_href
        try:
            resp = session.request("DELETE", url, timeout=30)
        finally:
            self._store(user, creds)
        if resp.status_code not in (200, 204, 404):
            raise GoogleApiError(f"delete {resp.status_code}: {resp.text[:300]}")
        return True


class GoogleNotConnected(Exception):
    pass


class GoogleApiError(Exception):
    pass


def _parse_addressbook(xml_bytes: bytes) -> list[dict]:
    """Parse a CardDAV multistatus into [{google_href, etag, vcard, name}]."""
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    D, C = "{DAV:}", "{urn:ietf:params:xml:ns:carddav}"
    for resp in root.findall(f"{D}response"):
        href_el = resp.find(f"{D}href")
        if href_el is None or not href_el.text:
            continue
        etag, vcard = "", ""
        for propstat in resp.findall(f"{D}propstat"):
            prop = propstat.find(f"{D}prop")
            if prop is None:
                continue
            et = prop.find(f"{D}getetag")
            if et is not None and et.text:
                etag = et.text
            ad = prop.find(f"{C}address-data")
            if ad is not None and ad.text:
                vcard = ad.text
        if not vcard:
            continue  # the collection entry itself, or no data
        out.append({"google_href": href_el.text, "etag": etag,
                    "vcard": vcard, "name": _vcard_fn(vcard)})
    return out


def _vcard_fn(vcard_text: str) -> str:
    for line in vcard_text.splitlines():
        if line.upper().startswith("FN:"):
            return line[3:].strip() or "Contact"
    return "Contact"
