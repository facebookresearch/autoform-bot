from __future__ import annotations

import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from autoform_cli.claims import ClaimTransportError, author_claim_key
from autoform_cli.dashboard import (
    DashboardHandler,
    LIVE_ENDPOINT,
    LIVE_SCHEMA,
    build_live_state,
    live_state_loader,
    publication_bound_live_state,
    serve_dashboard,
)
from autoform_cli.render import publication_source_revision


def _runtime():
    return SimpleNamespace(
        source_revision="revision",
        nodes=(
            SimpleNamespace(
                id="chapter/result",
                article_id="af_000000000000000000000001",
                title="Main result",
            ),
        ),
    )


def test_live_state_projects_only_current_author_claims() -> None:
    key = author_claim_key("af_000000000000000000000001")
    state = build_live_state(
        _runtime(),  # type: ignore[arg-type]
        [
            {
                "_key": key,
                "_malformed": False,
                "_expired": False,
                "owner": "worker-a",
                "expires_at": 123.0,
                "note": "proof attempt 1",
            },
            {
                "_key": "lake-build",
                "_malformed": False,
                "_expired": False,
                "owner": "worker-b",
            },
            {
                "_key": key,
                "_malformed": False,
                "_expired": True,
                "owner": "stale-worker",
            },
        ],
    )

    assert state == {
        "schema": LIVE_SCHEMA,
        "source_revision": "revision",
        "claims": [
            {
                "node_id": "chapter/result",
                "article_id": "af_000000000000000000000001",
                "title": "Main result",
                "owner": "worker-a",
                "expires_at": 123.0,
                "note": "proof attempt 1",
            }
        ],
    }


def test_live_loader_reports_claim_failures_without_breaking_static_site() -> None:
    class BrokenClaims:
        def list(self) -> list[dict[str, object]]:
            raise ClaimTransportError("offline")

    state = live_state_loader(_runtime, BrokenClaims())()

    assert state["schema"] == LIVE_SCHEMA
    assert state["claims"] == []
    assert state["error"] == "ClaimTransportError: offline"


def test_live_loader_does_not_hide_programming_errors() -> None:
    class BuggyClaims:
        def list(self) -> list[dict[str, object]]:
            raise TypeError("bug")

    with pytest.raises(TypeError, match="bug"):
        live_state_loader(_runtime, BuggyClaims())()


def test_live_overlay_refuses_a_stale_built_publication(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    article = roadmap / "README.md"
    article.write_text("# Roadmap\n", encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()
    (site / "publication.json").write_text(
        json.dumps(
            {
                "schema": "autoform-publication/v1",
                "complete": True,
                "source_revision": publication_source_revision(blueprint),
            }
        ),
        encoding="utf-8",
    )

    class EmptyClaims:
        def list(self) -> list[dict[str, object]]:
            return []

    state = publication_bound_live_state(
        _runtime,
        EmptyClaims(),
        blueprint_dir=blueprint,
        site_dir=site,
    )

    assert state()["source_revision"] == "revision"
    article.write_text("# Changed\n", encoding="utf-8")
    stale = state()
    assert stale["claims"] == []
    assert "stale" in str(stale["error"])


def test_dashboard_handler_serves_static_site_and_no_store_overlay(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("dashboard", encoding="utf-8")
    state = {"schema": LIVE_SCHEMA, "source_revision": "revision", "claims": []}
    handler = partial(DashboardHandler, directory=str(site), live_state=lambda: state)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/", timeout=5) as response:  # noqa: S310
            assert response.read() == b"dashboard"
        with urlopen(f"http://{host}:{port}{LIVE_ENDPOINT}", timeout=5) as response:  # noqa: S310
            assert json.loads(response.read()) == state
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_refuses_non_loopback_or_missing_site(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        serve_dashboard(tmp_path, lambda: {}, host="0.0.0.0")
    with pytest.raises(ValueError, match="does not exist"):
        serve_dashboard(tmp_path / "missing", lambda: {})
    with pytest.raises(ValueError, match="no live overlay asset"):
        serve_dashboard(tmp_path, lambda: {})


def test_localhost_is_bound_as_literal_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import dashboard

    site = tmp_path / "site"
    asset = site / "javascripts/blueprint-live.js"
    asset.parent.mkdir(parents=True)
    asset.write_text("", encoding="utf-8")
    observed: dict[str, object] = {}

    class FakeServer:
        server_address = ("127.0.0.1", 43210)

        def __init__(self, address, handler) -> None:
            observed.update(address=address, handler=handler)

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def serve_forever(self) -> None:
            observed["served"] = True

    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", FakeServer)
    dashboard.serve_dashboard(
        site,
        lambda: {},
        host="localhost",
        on_ready=lambda host, port: observed.update(ready=(host, port)),
    )

    assert observed["address"] == ("127.0.0.1", 0)
    assert observed["ready"] == ("127.0.0.1", 43210)
    assert observed["served"] is True


def test_dashboard_cli_resolves_project_site_and_stays_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autoform_cli import __main__ as cli

    project = tmp_path / "project"
    (project / "blueprint/roadmap").mkdir(parents=True)
    site = project / "built-site"
    site.mkdir()
    observed: dict[str, object] = {}

    def serve(site_dir: Path, state, *, host: str, port: int, on_ready) -> None:
        observed.update(site=site_dir, state=state, host=host, port=port)
        on_ready(host, 43210)

    monkeypatch.setattr(cli, "serve_dashboard", serve)

    assert cli.main(
        [
            "dashboard",
            str(project),
            "--site-dir",
            "built-site",
            "--repo",
            str(tmp_path / "claims.git"),
            "--scratch",
            str(tmp_path / "scratch"),
        ]
    ) == 0
    assert observed["site"] == site
    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 0
    assert callable(observed["state"])
    assert "http://127.0.0.1:43210/" in capsys.readouterr().out
