"""Compose the web control panel (/) with the CardDAV endpoint (/dav)."""
from __future__ import annotations

from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .google_proxy import GatewayMiddleware, GoogleProxy
from .webapp import create_flask_app


def create_wsgi_app(cfg: Config, service, radicale_app, google_link=None):
    flask_app = create_flask_app(cfg, service, google_link)
    dav_app = radicale_app
    # When Google is configured, front Radicale with the CardDAV gateway so the
    # `google` address book proxies live to Google and shows up in discovery.
    if google_link is not None and google_link.enabled:
        dav_app = GatewayMiddleware(radicale_app, GoogleProxy(cfg, google_link))
    application = DispatcherMiddleware(flask_app, {"/dav": dav_app})
    if cfg.reverse_proxy:
        n = cfg.trusted_proxy_count
        application = ProxyFix(application, x_for=n, x_proto=n, x_host=n, x_prefix=n)
    return application
