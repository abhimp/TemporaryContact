#!/usr/bin/env python3
"""Entry point: load config, start the retention worker, serve the app.

    python run.py -c config.yml            # run the server
    python run.py -c config.yml --cleanup  # run one retention pass and exit (cron)
"""
from __future__ import annotations

import argparse
import logging

from temporarycontacts.config import load_config
from temporarycontacts.db import Database
from temporarycontacts.radicale_app import build_radicale, build_storage
from temporarycontacts.retention import RetentionService, RetentionWorker
from temporarycontacts.wsgi import create_wsgi_app


def build(cfg):
    db = Database(cfg)
    radicale_app, configuration = build_radicale(cfg)
    storage = build_storage(configuration)
    service = RetentionService(cfg, storage, db)
    return db, radicale_app, service


def serve(cfg, app):
    if cfg.use_standalone_tls:
        from cheroot.ssl.builtin import BuiltinSSLAdapter
        from cheroot.wsgi import Server as WSGIServer
        server = WSGIServer((cfg.bind_host, cfg.bind_port), app)
        server.ssl_adapter = BuiltinSSLAdapter(cfg.tls_cert, cfg.tls_key)
        logging.info("Serving HTTPS on %s:%s", cfg.bind_host, cfg.bind_port)
        try:
            server.start()
        except KeyboardInterrupt:
            server.stop()
    else:
        from waitress import serve as waitress_serve
        logging.info("Serving HTTP on %s:%s (expecting a TLS reverse proxy)",
                     cfg.bind_host, cfg.bind_port)
        waitress_serve(app, host=cfg.bind_host, port=cfg.bind_port)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Temporary Contacts server")
    parser.add_argument("-c", "--config", default="config.yml")
    parser.add_argument("--cleanup", action="store_true",
                        help="run one retention pass and exit (for cron)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    _, radicale_app, service = build(cfg)

    if args.cleanup:
        service.run_once()
        return

    RetentionWorker(service).start()
    app = create_wsgi_app(cfg, service, radicale_app)
    serve(cfg, app)


if __name__ == "__main__":
    main()
