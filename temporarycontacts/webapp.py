"""Flask web control panel: login, list contacts, set retention, delete."""
from __future__ import annotations

import json
import os
import secrets
from functools import wraps

from flask import (Flask, Response, flash, redirect, render_template, request,
                   send_file, session, url_for)
from passlib.apache import HtpasswdFile

from .config import Config
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


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def create_flask_app(cfg: Config, service: RetentionService) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = _secret_key(cfg)

    @app.context_processor
    def inject_globals():
        return {
            "server_name": cfg.server_name,
            "units": UNITS,
            "format_left": format_left,
            "decompose": decompose,
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
        return render_template("settings.html", amount=amount, unit=unit)

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
