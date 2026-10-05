"""Serve the published blueprint with a loopback-only live claim overlay."""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from .claims import ClaimTransportError, author_claim_key
from .graph import GraphValidationError
from .render import (
    LIVE_SCRIPT,
    PUBLICATION_MANIFEST,
    PUBLICATION_SCHEMA,
    publication_source_revision,
)
from .runtime import RuntimeGraph, RuntimeProjectionError


LIVE_SCHEMA = "autoform-live/v1"
LIVE_ENDPOINT = "/__autoform/live.json"


class ClaimReader(Protocol):
    def list(self) -> list[dict[str, object]]: ...


def build_live_state(runtime: RuntimeGraph, leases: list[dict[str, object]]) -> dict[str, object]:
    """Project live author claims onto nodes without creating durable state."""

    claims_by_key = {
        author_claim_key(getattr(node, "article_id", None) or node.id): node
        for node in runtime.nodes
    }
    claims: list[dict[str, object]] = []
    for lease in leases:
        key = lease.get("_key")
        node = claims_by_key.get(key) if isinstance(key, str) else None
        if (
            node is None
            or lease.get("_malformed") is not False
            or lease.get("_expired") is not False
            or not isinstance(lease.get("owner"), str)
        ):
            continue
        item: dict[str, object] = {
            "node_id": node.id,
            "title": node.title,
            "owner": lease["owner"],
        }
        for field in ("expires_at", "note"):
            value = lease.get(field)
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                item[field] = value
        claims.append(item)
    return {
        "schema": LIVE_SCHEMA,
        "source_revision": runtime.source_revision,
        "claims": sorted(claims, key=lambda claim: str(claim["node_id"])),
    }


def live_state_loader(
    runtime_loader: Callable[[], RuntimeGraph],
    claims: ClaimReader,
) -> Callable[[], dict[str, object]]:
    """Return a request-time loader whose failures remain local and explicit."""

    lock = threading.Lock()

    def load() -> dict[str, object]:
        with lock:
            try:
                return build_live_state(runtime_loader(), claims.list())
            except (
                ClaimTransportError,
                GraphValidationError,
                OSError,
                RuntimeProjectionError,
                ValueError,
            ) as error:
                return {
                    "schema": LIVE_SCHEMA,
                    "claims": [],
                    "error": f"{type(error).__name__}: {error}",
                }

    return load


def publication_bound_live_state(
    runtime_loader: Callable[[], RuntimeGraph],
    claims: ClaimReader,
    *,
    blueprint_dir: str | Path,
    site_dir: str | Path,
) -> Callable[[], dict[str, object]]:
    """Refuse live badges when the built publication is stale or incomplete."""

    load = live_state_loader(runtime_loader, claims)
    blueprint = Path(blueprint_dir)
    manifest_path = Path(site_dir) / PUBLICATION_MANIFEST

    def guarded() -> dict[str, object]:
        try:
            encoded = manifest_path.read_bytes()
            if len(encoded) > 64 * 1024:
                raise ValueError("publication manifest is too large")
            manifest = json.loads(encoded)
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema") != PUBLICATION_SCHEMA
                or manifest.get("complete") is not True
                or manifest.get("source_revision") != publication_source_revision(blueprint)
            ):
                raise ValueError("built dashboard is stale; rerun render and the MkDocs build")
        except (OSError, ValueError) as error:
            return {
                "schema": LIVE_SCHEMA,
                "claims": [],
                "error": str(error),
            }
        return load()

    return guarded


class DashboardHandler(SimpleHTTPRequestHandler):
    """Static site handler with one no-store JSON endpoint."""

    def __init__(
        self,
        *args: object,
        live_state: Callable[[], dict[str, object]],
        **kwargs: object,
    ) -> None:
        self.live_state = live_state
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if urlsplit(self.path).path != LIVE_ENDPOINT:
            super().do_GET()
            return
        encoded = json.dumps(
            self.live_state(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)


def serve_dashboard(
    site_dir: str | Path,
    live_state: Callable[[], dict[str, object]],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    on_ready: Callable[[str, int], None] | None = None,
) -> None:
    """Serve one built site and ephemeral overlay on a loopback address."""

    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("dashboard host must be loopback-only (127.0.0.1 or localhost)")
    site = Path(site_dir).expanduser()
    if site.is_symlink() or not site.resolve().is_dir():
        raise ValueError("dashboard site directory does not exist or is a symbolic link")
    if not (site.resolve() / LIVE_SCRIPT).is_file():
        raise ValueError("built site has no live overlay asset; rerun render and the MkDocs build")
    handler = partial(
        DashboardHandler,
        directory=str(site.resolve()),
        live_state=live_state,
    )
    bind_host = "127.0.0.1" if host == "localhost" else host
    with ThreadingHTTPServer((bind_host, port), handler) as server:
        bound_host, bound_port = server.server_address[:2]
        if on_ready is not None:
            on_ready(str(bound_host), int(bound_port))
        server.serve_forever()


__all__ = [
    "DashboardHandler",
    "LIVE_ENDPOINT",
    "LIVE_SCHEMA",
    "build_live_state",
    "live_state_loader",
    "publication_bound_live_state",
    "serve_dashboard",
]
