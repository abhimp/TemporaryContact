"""Load and represent the single config.yml."""
from __future__ import annotations

import os
from dataclasses import dataclass

import yaml


@dataclass
class Config:
    # Branding
    server_name: str = "Temporary Contacts"
    logo_path: str = ""
    domain_name: str = "localhost"

    # Networking
    bind_host: str = "127.0.0.1"
    bind_port: int = 5232
    base_path: str = "/"
    reverse_proxy: bool = True
    trusted_proxy_count: int = 1

    # TLS (standalone mode)
    tls_cert: str = ""
    tls_key: str = ""

    # Auth
    htpasswd_path: str = "./data/users.htpasswd"
    secret_key: str = ""

    # Storage
    storage_path: str = "./data/collections"

    # Retention
    default_retention_days: float = 7.0
    scan_interval_seconds: int = 300
    # How long deleted/expired contacts stay recoverable in the "Deleted" archive.
    trash_retention_days: float = 60.0

    # Database
    database_type: str = "sqlite"
    database_url: str = ""
    database_path: str = "./data/app.db"
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_name: str = "temporarycontacts"
    pg_user: str = "temporarycontacts"
    pg_password: str = ""

    # Google integration ("Keep / Save to Google")
    google_enabled: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""

    @property
    def default_retention_seconds(self) -> float:
        return self.default_retention_days * 86400.0

    @property
    def google_callback_url(self) -> str:
        """Public OAuth redirect URI; derived from the domain if not set."""
        if self.google_redirect_uri:
            return self.google_redirect_uri
        return f"https://{self.domain_name}/google/callback"

    @property
    def use_standalone_tls(self) -> bool:
        return (not self.reverse_proxy) and bool(self.tls_cert and self.tls_key)

    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        if self.database_type == "postgresql":
            pw = f":{self.pg_password}" if self.pg_password else ""
            return (f"postgresql+psycopg://{self.pg_user}{pw}"
                    f"@{self.pg_host}:{self.pg_port}/{self.pg_name}")
        return f"sqlite:///{os.path.abspath(self.database_path)}"


def load_config(path: str) -> Config:
    path = os.path.abspath(path)
    base = os.path.dirname(path)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    def resolve(p: str) -> str:
        if not p:
            return ""
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    net = data.get("network", {}) or {}
    tls = data.get("tls", {}) or {}
    auth = data.get("auth", {}) or {}
    storage = data.get("storage", {}) or {}
    retention = data.get("retention", {}) or {}
    db = data.get("database", {}) or {}
    google = data.get("google", {}) or {}

    base_path = net.get("base_path", "/") or "/"
    if not base_path.startswith("/"):
        base_path = "/" + base_path
    if not base_path.endswith("/"):
        base_path += "/"

    return Config(
        server_name=data.get("server_name", "Temporary Contacts"),
        logo_path=resolve(data.get("logo_path", "")),
        domain_name=data.get("domain_name", "localhost"),
        bind_host=net.get("bind_host", "127.0.0.1"),
        bind_port=int(net.get("bind_port", 5232)),
        base_path=base_path,
        reverse_proxy=bool(net.get("reverse_proxy", True)),
        trusted_proxy_count=int(net.get("trusted_proxy_count", 1)),
        tls_cert=resolve(tls.get("cert", "")),
        tls_key=resolve(tls.get("key", "")),
        htpasswd_path=resolve(auth.get("htpasswd_path", "./data/users.htpasswd")),
        secret_key=auth.get("secret_key", "") or "",
        storage_path=resolve(storage.get("path", "./data/collections")),
        default_retention_days=float(retention.get("default_days", 7)),
        scan_interval_seconds=int(retention.get("scan_interval_seconds", 300)),
        trash_retention_days=float(retention.get("trash_retention_days", 60)),
        database_type=db.get("type", "sqlite"),
        database_url=db.get("url", "") or "",
        database_path=resolve(db.get("path", "./data/app.db")),
        pg_host=db.get("host", "localhost"),
        pg_port=int(db.get("port", 5432)),
        pg_name=db.get("name", "temporarycontacts"),
        pg_user=db.get("user", "temporarycontacts"),
        pg_password=db.get("password", "") or "",
        google_enabled=bool(google.get("enabled", False)),
        google_client_id=google.get("client_id", "") or "",
        google_client_secret=google.get("client_secret", "") or "",
        google_redirect_uri=google.get("redirect_uri", "") or "",
    )
