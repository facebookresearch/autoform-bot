"""Serve the published blueprint on a loopback address."""

from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from ..lean import PUBLICATION_MANIFEST


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def _local_authority(value: str, port: int) -> bool:
    """Accept one literal loopback Host authority for this exact server port."""

    try:
        parsed = urlsplit(f"//{value}")
        candidate_port = parsed.port
    except ValueError:
        return False
    return (
        parsed.hostname in _LOOPBACK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and (candidate_port if candidate_port is not None else 80) == port
    )


def _local_origin(value: str, port: int) -> bool:
    """Accept an HTTP Origin matching the dashboard's loopback authority."""

    try:
        parsed = urlsplit(value)
        candidate_port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in _LOOPBACK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and (candidate_port if candidate_port is not None else 80) == port
    )


class DashboardHandler(SimpleHTTPRequestHandler):
    """Static site handler that answers only its own loopback authority."""

    def _accept_local_request(self) -> bool:
        """Reject nonlocal browser authority before any response surface."""

        port = int(self.server.server_address[1])
        hosts = self.headers.get_all("Host", failobj=[])
        if len(hosts) != 1 or not _local_authority(hosts[0], port):
            self.send_error(421, "dashboard Host must match its loopback listener")
            return False
        origins = self.headers.get_all("Origin", failobj=[])
        if len(origins) > 1 or (origins and not _local_origin(origins[0], port)):
            self.send_error(403, "dashboard Origin must match its loopback listener")
            return False
        return True

    def do_HEAD(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self._accept_local_request():
            super().do_HEAD()

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self._accept_local_request():
            super().do_GET()


def serve_dashboard(
    site_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    on_ready: Callable[[str, int], None] | None = None,
) -> None:
    """Serve one built site on a loopback address."""

    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("dashboard host must be loopback-only (127.0.0.1 or localhost)")
    site = Path(site_dir).expanduser()
    if site.is_symlink() or not site.resolve().is_dir():
        raise ValueError("dashboard site directory does not exist or is a symbolic link")
    if not (site.resolve() / PUBLICATION_MANIFEST).is_file():
        raise ValueError(f"built site has no {PUBLICATION_MANIFEST}; rerun render and the MkDocs build")
    handler = partial(DashboardHandler, directory=str(site.resolve()))
    bind_host = "127.0.0.1" if host == "localhost" else host
    with ThreadingHTTPServer((bind_host, port), handler) as server:
        bound_host, bound_port = server.server_address[:2]
        if on_ready is not None:
            on_ready(str(bound_host), int(bound_port))
        server.serve_forever()


__all__ = [
    "DashboardHandler",
    "serve_dashboard",
]
