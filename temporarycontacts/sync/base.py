"""Interface for future external sync providers (Google, Outlook, ...).

A provider mirrors contacts between the local CardDAV store and an external
address book. Implementations are intentionally deferred; this defines the
contract the core will call so they slot in later without a rewrite.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class SyncResult:
    pulled: int = 0
    pushed: int = 0
    deleted: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class SyncProvider(abc.ABC):
    """One external account to sync a user's temporary contacts with."""

    #: short identifier, e.g. "google", "outlook"
    type: str = "base"

    def __init__(self, user: str, options: dict):
        self.user = user
        self.options = options

    @abc.abstractmethod
    def sync(self, storage) -> SyncResult:
        """Reconcile the external address book with the local CardDAV store."""
        raise NotImplementedError


def build_provider(user: str, spec: dict) -> SyncProvider:
    """Factory — will map spec['type'] to a concrete provider once implemented."""
    raise NotImplementedError(
        f"Sync provider {spec.get('type')!r} is not implemented yet."
    )
