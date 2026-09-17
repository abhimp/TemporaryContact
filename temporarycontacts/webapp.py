"""Flask web control panel: login, list contacts, set retention, delete."""
from __future__ import annotations

import json
import os
import secrets
from functools import wraps

import vobject

from flask import (Flask, Response, flash, redirect, render_template, request,
                   send_file, session, url_for)
from passlib.apache import HtpasswdFile

from . import contacts as contacts_api
from .config import Config
from .google_link import GoogleApiError, GoogleLink, GoogleNotConnected
from .retention import RetentionService

UNIT_SECONDS = {"hours": 3600, "days": 86400, "weeks": 604800}
UNITS = [("hours", "Hours"), ("days", "Days"), ("weeks", "Weeks")]

DEFAULT_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    '<rect width="512" height="512" rx="96" fill="#2563eb"/>'
    '<circle cx="256" cy="256" r="150" fill="none" stroke="#fff" stroke-width="28"/>'
    '<path d="M256 168v96l64 40" fill="none" stroke="#fff" '
    'stroke-width="28" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def _secret_key(cfg: Config) -> str:
    if cfg.secret_key:
        return cfg.secret_key
    data_dir = os.path.dirname(os.path.abspath(cfg.storage_path)) or "."
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "secret.key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key)
    os.chmod(path, 0o600)
    return key


def _check_password(cfg: Config, user: str, password: str) -> bool:
    if not user or not os.path.exists(cfg.htpasswd_path):
        return False
    try:
        ht = HtpasswdFile(cfg.htpasswd_path)
    except Exception:
        return False
    return bool(ht.check_password(user, password))


def _parse_seconds(form) -> float | None:
    try:
        amount = float(form.get("amount", ""))
    except (TypeError, ValueError):
        return None
    unit = form.get("unit", "days")
    if amount <= 0 or unit not in UNIT_SECONDS:
        return None
    return amount * UNIT_SECONDS[unit]


def format_left(seconds) -> str:
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "Expired"
    minutes = int(seconds // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h left"
    if hours:
        return f"{hours}h {mins}m left"
    return f"{max(mins, 1)}m left"


def decompose(seconds) -> tuple[int, str]:
    sec = int(seconds or 0)
    if sec > 0 and sec % 604800 == 0:
        return sec // 604800, "weeks"
    if sec > 0 and sec % 86400 == 0:
        return sec // 86400, "days"
    if sec > 0 and sec % 3600 == 0:
        return sec // 3600, "hours"
    return max(int(round(sec / 86400)), 1), "days"


def _vcard_to_form(vobj) -> dict:
    """Flatten a vCard into simple editable fields for the form."""
    given = family = org = title = ""
    if hasattr(vobj, "n") and vobj.n.value:
        given = vobj.n.value.given or ""
        family = vobj.n.value.family or ""
    if hasattr(vobj, "org") and vobj.org.value:
        v = vobj.org.value
        org = v[0] if isinstance(v, list) and v else (v or "")
    if hasattr(vobj, "title") and vobj.title.value:
        title = vobj.title.value
    name = vobj.fn.value if hasattr(vobj, "fn") else f"{given} {family}".strip()
    join = lambda key: "\n".join(c.value for c in vobj.contents.get(key, []))
    return {"given": given, "family": family, "organization": org, "title": title,
            "phones": join("tel"), "emails": join("email"), "urls": join("url"),
            "name": name}


def _apply_form_to_vcard(vobj, form) -> str:
    """Apply edited fields onto an existing vCard, preserving untouched ones."""
    given = form.get("given", "").strip()
    family = form.get("family", "").strip()
    org = form.get("organization", "").strip()
    title = form.get("title", "").strip()

    if not hasattr(vobj, "n"):
        vobj.add("n")
    vobj.n.value = vobject.vcard.Name(family=family, given=given)
    if not hasattr(vobj, "fn"):
        vobj.add("fn")
    vobj.fn.value = f"{given} {family}".strip() or org or "Contact"

    def set_single(key, value):
        if value:
            if not hasattr(vobj, key):
                vobj.add(key)
            getattr(vobj, key).value = [value] if key == "org" else value
        elif key in vobj.contents:
            del vobj.contents[key]

    set_single("org", org)
    set_single("title", title)

    def set_multi(key, text):
        while key in vobj.contents:
            del vobj.contents[key]
        for line in text.splitlines():
            line = line.strip()
            if line:
                vobj.add(key).value = line

    set_multi("tel", form.get("phones", ""))
    set_multi("email", form.get("emails", ""))
    set_multi("url", form.get("urls", ""))
    return vobj.serialize()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def create_flask_app(cfg: Config, service: RetentionService,
                     google_link: GoogleLink | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = _secret_key(cfg)

    @app.context_processor
    def inject_globals():
        return {
            "server_name": cfg.server_name,
            "units": UNITS,
            "format_left": format_left,
            "decompose": decompose,
            "google_enabled": bool(google_link and google_link.enabled),
        }

    @app.route("/")
    def index():
        return redirect(url_for("contacts") if "user" in session else url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            user = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if _check_password(cfg, user, password):
                session["user"] = user
                return redirect(url_for("contacts"))
            flash("Invalid username or password.")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/contacts")
    @login_required
    def contacts():
        items = service.list_for_user(session["user"])
        return render_template("contacts.html", contacts=items)

    @app.route("/contacts/<addressbook>/<href>/retention", methods=["POST"])
    @login_required
    def set_retention(addressbook, href):
        seconds = _parse_seconds(request.form)
        if seconds:
            service.set_retention(session["user"], addressbook, href, seconds)
            flash("Retention updated.")
        else:
            flash("Enter a valid duration.")
        return redirect(url_for("contacts"))

    @app.route("/contacts/<addressbook>/<href>/delete", methods=["POST"])
    @login_required
    def delete_contact(addressbook, href):
        service.delete_now(session["user"], addressbook, href)
        flash("Contact deleted.")
        return redirect(url_for("contacts"))

    @app.route("/contacts/<addressbook>/<href>/keep", methods=["POST"])
    @login_required
    def keep_contact(addressbook, href):
        user = session["user"]
        if not (google_link and google_link.enabled):
            flash("Google is not configured on this server.")
            return redirect(url_for("contacts"))
        if not google_link.connection(user):
            flash("Connect your Google account first (Settings).")
            return redirect(url_for("settings"))
        try:
            service.link_contact(user, addressbook, href)
        except GoogleNotConnected:
            flash("Connect your Google account first (Settings).")
            return redirect(url_for("settings"))
        except LookupError:
            flash("That contact no longer exists.")
            return redirect(url_for("contacts"))
        except GoogleApiError as exc:
            flash(f"Google rejected the contact: {exc}")
            return redirect(url_for("contacts"))
        flash("Saved to Google Contacts. It stays synced and no longer expires.")
        return redirect(url_for("contacts"))

    @app.route("/contacts/<addressbook>/<href>/edit", methods=["GET", "POST"])
    @login_required
    def edit_contact(addressbook, href):
        user = session["user"]
        vobj, _etag = contacts_api.get_contact(service.storage, user, addressbook, href)
        if vobj is None:
            flash("That contact no longer exists.")
            return redirect(url_for("contacts"))
        if request.method == "POST":
            text = _apply_form_to_vcard(vobj, request.form)
            contacts_api.save_contact(service.storage, user, addressbook, href, text)
            service.push_after_edit(user, addressbook, href)
            flash("Contact updated.")
            return redirect(url_for("contacts"))
        return render_template("edit.html", form=_vcard_to_form(vobj),
                               addressbook=addressbook, href=href)

    @app.route("/google/connect")
    @login_required
    def google_connect():
        if not (google_link and google_link.enabled):
            flash("Google is not configured on this server.")
            return redirect(url_for("settings"))
        auth_url, state = google_link.authorization_url()
        session["google_oauth_state"] = state
        return redirect(auth_url)

    @app.route("/google/callback")
    @login_required
    def google_callback():
        state = session.pop("google_oauth_state", None)
        if not (google_link and google_link.enabled) or not state:
            flash("Google sign-in could not be completed.")
            return redirect(url_for("settings"))
        try:
            email = google_link.finish_authorization(
                session["user"], request.url, state)
        except Exception as exc:  # noqa: BLE001 — surface any OAuth failure
            flash(f"Google sign-in failed: {exc}")
            return redirect(url_for("settings"))
        flash(f"Connected Google account{' (' + email + ')' if email else ''}.")
        return redirect(url_for("settings"))

    @app.route("/google/disconnect", methods=["POST"])
    @login_required
    def google_disconnect():
        if google_link:
            google_link.disconnect(session["user"])
        flash("Disconnected Google account.")
        return redirect(url_for("settings"))

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        if request.method == "POST":
            seconds = _parse_seconds(request.form)
            if seconds:
                service.set_default_seconds(seconds)
                flash("Default retention updated.")
            else:
                flash("Enter a valid duration.")
            return redirect(url_for("settings"))
        amount, unit = decompose(service.default_seconds())
        google_conn = (google_link.connection(session["user"])
                       if google_link and google_link.enabled else None)
        return render_template("settings.html", amount=amount, unit=unit,
                               google_connection=google_conn)

    @app.route("/manifest.webmanifest")
    def manifest():
        root = request.script_root or ""
        data = {
            "name": cfg.server_name,
            "short_name": cfg.server_name[:12],
            "start_url": root + "/",
            "scope": root + "/",
            "display": "standalone",
            "background_color": "#111827",
            "theme_color": "#2563eb",
            "icons": [{"src": root + "/logo", "sizes": "512x512", "purpose": "any"}],
        }
        return Response(json.dumps(data), mimetype="application/manifest+json")

    @app.route("/logo")
    def logo():
        if cfg.logo_path and os.path.exists(cfg.logo_path):
            return send_file(cfg.logo_path)
        return Response(DEFAULT_LOGO_SVG, mimetype="image/svg+xml")

    # iOS auto-discovery (RFC 6764) issues PROPFIND — not GET — to this URL and
    # expects a redirect to the real CardDAV endpoint. Accept every method so the
    # PROPFIND isn't rejected with 405, and use 301 per the spec.
    @app.route("/.well-known/carddav",
               methods=["GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT"])
    def wellknown_carddav():
        return redirect((request.script_root or "") + "/dav/", code=301)

    return app
