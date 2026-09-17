"""Google integration: OAuth + "Keep / Save to Google" via the People API.

Per-user OAuth. When a user clicks Keep, the contact's vCard is converted to a
Google People `Person` and created in their Google Contacts; the caller then
removes it from the Temporary account.
"""
from __future__ import annotations

import json
import logging

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .config import Config
from .db import Database, GoogleCredential

log = logging.getLogger("temporarycontacts.google")

SCOPES = ["https://www.googleapis.com/auth/contacts",
          "https://www.googleapis.com/auth/userinfo.email", "openid"]
PEOPLE_CREATE_URL = "https://people.googleapis.com/v1/people:createContact"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


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

    def authorization_url(self) -> tuple[str, str]:
        return self._flow().authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent")

    def finish_authorization(self, user: str, authorization_response_url: str,
                             state: str) -> str:
        flow = self._flow(state=state)
        flow.fetch_token(authorization_response=authorization_response_url)
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
        return Credentials.from_authorized_user_info(json.loads(token), scopes=SCOPES)

    def _fetch_email(self, creds: Credentials) -> str:
        try:
            r = AuthorizedSession(creds).get(USERINFO_URL, timeout=15)
            if r.ok:
                return r.json().get("email", "")
        except Exception:
            log.exception("Could not fetch Google account email")
        return ""

    # ---- the Keep action ----

    def create_contact(self, user: str, vobject_item) -> None:
        """Create the contact in the user's Google Contacts. Raises on failure."""
        creds = self._load_credentials(user)
        if creds is None:
            raise GoogleNotConnected()
        person = vcard_to_person(vobject_item)
        session = AuthorizedSession(creds)
        resp = session.post(PEOPLE_CREATE_URL, json=person, timeout=30)
        # Persist any refreshed token so we don't re-prompt next time.
        self._store(user, creds)
        if not resp.ok:
            raise GoogleApiError(f"{resp.status_code}: {resp.text[:300]}")


class GoogleNotConnected(Exception):
    pass


class GoogleApiError(Exception):
    pass


def _val(component, attr):
    return getattr(component, attr).value if hasattr(component, attr) else None


def vcard_to_person(v) -> dict:
    """Map a vobject vCard to a Google People API Person body."""
    person: dict = {}

    name: dict = {}
    if hasattr(v, "n") and v.n.value:
        n = v.n.value
        name = {"givenName": n.given or "", "familyName": n.family or "",
                "middleName": n.additional or "",
                "honorificPrefix": n.prefix or "", "honorificSuffix": n.suffix or ""}
    if hasattr(v, "fn") and v.fn.value:
        name["unstructuredName"] = v.fn.value
    if name:
        person["names"] = [name]

    phones = [{"value": c.value, "type": _label(c)} for c in v.contents.get("tel", [])]
    if phones:
        person["phoneNumbers"] = phones

    emails = [{"value": c.value, "type": _label(c)} for c in v.contents.get("email", [])]
    if emails:
        person["emailAddresses"] = emails

    urls = [{"value": c.value, "type": _label(c)} for c in v.contents.get("url", [])]
    if urls:
        person["urls"] = urls

    orgs = []
    for c in v.contents.get("org", []):
        val = c.value
        org_name = val[0] if isinstance(val, list) and val else (val or "")
        dept = val[1] if isinstance(val, list) and len(val) > 1 else ""
        org = {"name": org_name}
        if dept:
            org["department"] = dept
        orgs.append(org)
    if hasattr(v, "title") and v.title.value:
        if orgs:
            orgs[0]["title"] = v.title.value
        else:
            orgs = [{"title": v.title.value}]
    if orgs:
        person["organizations"] = orgs

    addresses = []
    for c in v.contents.get("adr", []):
        a = c.value
        addresses.append({
            "streetAddress": a.street or "", "city": a.city or "",
            "region": a.region or "", "postalCode": a.code or "",
            "country": a.country or "", "type": _label(c),
        })
    if addresses:
        person["addresses"] = addresses

    if hasattr(v, "bday") and v.bday.value:
        person["birthdays"] = [{"text": v.bday.value}]

    return person


def _label(component) -> str:
    """Best-effort human label from a vCard TYPE param."""
    try:
        types = component.params.get("TYPE", [])
        if types:
            return str(types[0]).capitalize()
    except Exception:
        pass
    return ""
