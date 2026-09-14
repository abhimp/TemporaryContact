"""Build the embedded Radicale CardDAV application and its storage handle."""
from __future__ import annotations

import os

from radicale import Application
from radicale import config as radicale_config
from radicale import storage as radicale_storage

from .config import Config


def build_radicale(cfg: Config):
    """Return (wsgi_app, configuration) for the CardDAV endpoint."""
    os.makedirs(cfg.storage_path, exist_ok=True)
    configuration = radicale_config.load(())
    configuration.update(
        {
            "storage": {"filesystem_folder": os.path.abspath(cfg.storage_path)},
            "auth": {
                "type": "htpasswd",
                "htpasswd_filename": os.path.abspath(cfg.htpasswd_path),
                "htpasswd_encryption": "autodetect",
            },
            # Each user can only see/modify their own collections.
            "rights": {"type": "owner_only"},
        },
        "temporarycontacts",
    )
    return Application(configuration), configuration


def build_storage(configuration):
    """A storage handle for enumerating/deleting contacts out-of-band."""
    return radicale_storage.load(configuration)
