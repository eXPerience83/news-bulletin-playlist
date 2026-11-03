"""Offline HTTP assertions forbidding provider calls and operational mutations."""

from __future__ import annotations

import socket
import sqlite3
from http.server import HTTPServer

import pytest

from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.source_admin import SourceAdminService


def forbid_effects(monkeypatch, service, server: HTTPServer):
    def forbidden(*args, **kwargs):
        pytest.fail("Portable download attempted an operational side effect")

    for cls, names in (
        (ManagedStateStore, ("load", "save")),
        (ManagedAdminService, ("snapshot", "finalize_known_provisioning")),
        (SourceAdminService, ("snapshot", "inspect", "audit", "audit_all")),
    ):
        for name in names:
            monkeypatch.setattr(cls, name, forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(service.auth, "get_access_token", forbidden)
    monkeypatch.setattr(service, "client_factory", forbidden)
    monkeypatch.setattr(service.public_client, "observe_episode", forbidden)
    monkeypatch.setattr(server.RequestHandlerClass, "_spotify_state", forbidden)
    monkeypatch.setattr(server.RequestHandlerClass, "_spotify_metrics", forbidden)
    monkeypatch.setattr(server.RequestHandlerClass, "_admin_diagnostics", forbidden)
    lifecycle = server.RequestHandlerClass.engine_lifecycle
    monkeypatch.setattr(lifecycle, "reconcile", forbidden)
    monkeypatch.setattr(lifecycle, "wake", forbidden, raising=False)
    connect = socket.socket.connect

    def local_only(sock, address):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            forbidden()
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
