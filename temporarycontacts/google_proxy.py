"""Reverse-proxy the `google` address book to Google's CardDAV endpoint.

The iPhone talks CardDAV to us. For the `google` collection we forward each
request straight to Google's CardDAV service, swapping auth (the phone's Basic
login → the user's Google OAuth token) and rewriting hrefs so the phone keeps
talking to us, not to Google directly. Google itself provides the vCards, etags,
and sync-tokens — we don't reimplement any of that.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
from urllib.parse import quote
from xml.etree import ElementTree as ET

from google.auth.transport.requests import AuthorizedSession
from passlib.apache import HtpasswdFile

log = logging.getLogger("temporarycontacts.google_proxy")

# Google CardDAV collection paths look like:
#   /carddav/v1/principals/<email>/lists/<listid>/<item>
# (both path-only and full-URL forms appear in responses; <listid> is often
# "default" but can be an id, so match any single path segment).
GOOGLE_LIST_PATH_RE = re.compile(
    r"(?:https://www\.googleapis\.com)?/carddav/v1/principals/[^/]+/lists/[^/]+/")

# Request headers we must NOT forward to Google.
_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "authorization",
    "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
    "accept-encoding",
}
# Response headers we must not pass back verbatim.
_STRIP_RESPONSE = {
    "content-length", "content-encoding", "transfer-encoding", "connection",
    "keep-alive",
}

for _prefix, _uri in (("d", "DAV:"),
                      ("card", "urn:ietf:params:xml:ns:carddav"),
                      ("cs", "http://calendarserver.org/ns/")):
    ET.register_namespace(_prefix, _uri)


class GoogleProxy:
    def __init__(self, cfg, google_link):
        self.cfg = cfg
        self.google_link = google_link

    # ---- auth of the incoming CardDAV request (Basic vs htpasswd) ----

    def authenticate(self, environ) -> str | None:
        header = environ.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Basic "):
            return None
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return None
        if not user or not os.path.exists(self.cfg.htpasswd_path):
            return None
        try:
            ht = HtpasswdFile(self.cfg.htpasswd_path)
        except Exception:
            return None
        return user if ht.check_password(user, pw) else None

    # ---- URL + body rewriting ----

    def _google_url(self, email: str, subpath: str) -> str:
        base = (f"https://www.googleapis.com/carddav/v1/principals/"
                f"{quote(email)}/lists/default")
        if not subpath.startswith("/"):
            subpath = "/" + subpath
        return base + subpath

    def _our_prefix(self, script_name: str, user: str) -> str:
        return f"{script_name}/{user}/google/"

    def rewrite_from_google(self, body: bytes, script_name: str, user: str) -> bytes:
        """Google hrefs → our hrefs, in a response body."""
        text = body.decode("utf-8", "replace")
        text = GOOGLE_LIST_PATH_RE.sub(self._our_prefix(script_name, user), text)
        return text.encode("utf-8")

    def rewrite_to_google(self, body: bytes, email: str, script_name: str,
                          user: str) -> bytes:
        """Our hrefs → Google hrefs, in a request body (e.g. multiget)."""
        text = body.decode("utf-8", "replace")
        google_prefix = (f"/carddav/v1/principals/{email}/lists/default/")
        text = text.replace(self._our_prefix(script_name, user), google_prefix)
        return text.encode("utf-8")

    def _forward_headers(self, environ) -> dict:
        headers = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                name = key[5:].replace("_", "-").lower()
                if name not in _HOP_BY_HOP:
                    headers[name] = value
        if environ.get("CONTENT_TYPE"):
            headers["content-type"] = environ["CONTENT_TYPE"]
        return headers

    def _response_headers(self, resp, body: bytes, script_name: str,
                          user: str) -> list:
        out = []
        for key, value in resp.headers.items():
            low = key.lower()
            if low in _STRIP_RESPONSE:
                continue
            if low in ("location", "content-location"):
                value = GOOGLE_LIST_PATH_RE.sub(self._our_prefix(script_name, user),
                                                value)
            out.append((key, value))
        out.append(("Content-Length", str(len(body))))
        return out

    @staticmethod
    def _read_body(environ) -> bytes:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        return environ["wsgi.input"].read(length)

    # ---- the proxy handler ----

    def handle(self, environ, start_response, script_name: str, user: str,
               subpath: str):
        auth_user = self.authenticate(environ)
        if not auth_user:
            start_response("401 Unauthorized",
                           [("WWW-Authenticate", 'Basic realm="Temporary Contacts"'),
                            ("Content-Length", "0")])
            return [b""]
        if auth_user != user:
            start_response("403 Forbidden", [("Content-Length", "0")])
            return [b""]

        conn = self.google_link.connection(user)
        creds = self.google_link._load_credentials(user)
        if creds is None or not conn or not conn.get("email"):
            start_response("502 Bad Gateway", [("Content-Length", "0")])
            return [b""]
        email = conn["email"]

        method = environ["REQUEST_METHOD"]
        body = self._read_body(environ)
        if body:
            body = self.rewrite_to_google(body, email, script_name, user)
        headers = self._forward_headers(environ)

        try:
            session = AuthorizedSession(creds)
            resp = session.request(method, self._google_url(email, subpath),
                                   headers=headers, data=body or None, timeout=60)
        except Exception as exc:  # noqa: BLE001
            log.exception("Google CardDAV proxy error")
            start_response("502 Bad Gateway", [("Content-Length", "0")])
            return [str(exc).encode("utf-8")[:0] or b""]
        finally:
            self.google_link._store(user, creds)  # persist any refreshed token

        log.info("google proxy: %s %s -> %s (%d bytes)", method,
                 subpath, resp.status_code, len(resp.content or b""))
        out = resp.content or b""
        ctype = resp.headers.get("Content-Type", "")
        if out and ("xml" in ctype or method in ("PROPFIND", "REPORT")):
            out = self.rewrite_from_google(out, script_name, user)
        start_response(f"{resp.status_code} {resp.reason}",
                       self._response_headers(resp, out, script_name, user))
        return [out]

    # ---- discovery: describe Google's collection for the home listing ----

    def describe_collection(self, user: str, propfind_body: bytes,
                            script_name: str):
        """Return a <response> Element for the Google book (href rewritten),
        or None. Used to inject the google collection into the home PROPFIND."""
        conn = self.google_link.connection(user)
        creds = self.google_link._load_credentials(user)
        if creds is None or not conn or not conn.get("email"):
            return None
        email = conn["email"]
        body = propfind_body or (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<propfind xmlns="DAV:"><prop><resourcetype/><displayname/></prop>'
            b'</propfind>')
        try:
            session = AuthorizedSession(creds)
            resp = session.request(
                "PROPFIND", self._google_url(email, "/"),
                headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                data=body, timeout=30)
        except Exception:  # noqa: BLE001
            log.exception("Failed describing Google collection")
            return None
        finally:
            self.google_link._store(user, creds)
        log.info("describe_collection: Google PROPFIND -> %s", resp.status_code)
        if resp.status_code != 207:
            log.warning("describe_collection non-207 body: %s", resp.text[:300])
            return None
        rewritten = self.rewrite_from_google(resp.content, script_name, user)
        try:
            ms = ET.fromstring(rewritten)
        except ET.ParseError:
            return None
        # Return the first <response> (the collection itself).
        return ms.find("{DAV:}response")


class GatewayMiddleware:
    """Routes `/<user>/google/*` to the proxy; injects the Google book into the
    home PROPFIND so iOS discovers both address books. Everything else falls
    through to Radicale unchanged."""

    _GOOGLE_RE = re.compile(r"^/([^/]+)/google(/.*)?$")
    _HOME_RE = re.compile(r"^/([^/]+)/?$")

    def __init__(self, app, proxy: GoogleProxy):
        self.app = app
        self.proxy = proxy

    def _google_available(self, user: str) -> bool:
        gl = self.proxy.google_link
        return bool(gl and gl.enabled and gl.connection(user))

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        script = environ.get("SCRIPT_NAME", "")

        method = environ.get("REQUEST_METHOD")
        m = self._GOOGLE_RE.match(path)
        if m:
            user, sub = m.group(1), (m.group(2) or "/")
            log.info("gateway: route to Google proxy: %s %s%s (sub=%s)",
                     method, script, path, sub)
            return self.proxy.handle(environ, start_response, script, user, sub)

        home = self._HOME_RE.match(path)
        if method == "PROPFIND" and home:
            log.info("gateway: home PROPFIND path=%s depth=%s user=%s google_avail=%s",
                     path, environ.get("HTTP_DEPTH"), home.group(1),
                     self._google_available(home.group(1)))
        if (method == "PROPFIND" and home
                and environ.get("HTTP_DEPTH") == "1"
                and self._google_available(home.group(1))):
            return self._inject_home(environ, start_response, script, home.group(1))

        return self.app(environ, start_response)

    def _inject_home(self, environ, start_response, script, user):
        body_in = GoogleProxy._read_body(environ)
        environ["wsgi.input"] = io.BytesIO(body_in)
        environ["CONTENT_LENGTH"] = str(len(body_in))
        # Ask the inner app (Radicale) for an uncompressed body so we can parse
        # and splice it; otherwise ET.fromstring() chokes on gzip bytes.
        environ.pop("HTTP_ACCEPT_ENCODING", None)

        captured, chunks = {}, []

        def capture_sr(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers
            return chunks.append

        result = self.app(environ, capture_sr)
        try:
            for chunk in result:
                chunks.append(chunk)
        finally:
            if hasattr(result, "close"):
                result.close()
        inner = b"".join(chunks)

        if not captured.get("status", "").startswith("207"):
            start_response(captured["status"], captured["headers"])
            return [inner]

        # Defensively decompress if the inner app compressed anyway.
        if inner[:2] == b"\x1f\x8b":
            import gzip as _gzip
            try:
                inner = _gzip.decompress(inner)
            except Exception:  # noqa: BLE001
                pass

        google_resp = self.proxy.describe_collection(user, body_in, script)
        log.info("gateway: home inject describe_collection -> %s",
                 "ok" if google_resp is not None else "None")
        if google_resp is not None:
            try:
                root = ET.fromstring(inner)
                root.append(google_resp)
                inner = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                log.info("gateway: spliced Google book into home listing")
            except ET.ParseError:
                log.warning("Could not splice Google collection into home listing")

        # We return an identity (uncompressed) body, so drop any encoding header.
        headers = [(k, v) for k, v in captured["headers"]
                   if k.lower() not in ("content-length", "content-encoding")]
        headers.append(("Content-Length", str(len(inner))))
        start_response(captured["status"], headers)
        return [inner]
