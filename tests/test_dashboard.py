from __future__ import annotations

import http.client
import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

import autoform_cli._tree_snapshot as tree_snapshot_module
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
from autoform_cli.render import PUBLICATION_SCHEMA, publication_source_revision


def _runtime():
    return SimpleNamespace(
        source_revision="revision",
        nodes=(
            SimpleNamespace(
                id="chapter/result",
                article_id="af_000000000000000000000001",
                title="Main result",
            ),
            SimpleNamespace(id="chapter/other", article_id=None, title="Other result"),
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
                "_key": author_claim_key("chapter/result"),
                "_malformed": False,
                "_expired": False,
                "owner": "path-worker",
            },
            {
                "_key": author_claim_key("chapter/other"),
                "_malformed": False,
                "_expired": False,
                "owner": "worker-c",
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
                "node_id": "chapter/other",
                "title": "Other result",
                "owner": "worker-c",
                "claim_target": "chapter/other",
            },
            {
                "node_id": "chapter/result",
                "article_id": "af_000000000000000000000001",
                "title": "Main result",
                "owner": "worker-a",
                "claim_target": "af_000000000000000000000001",
                "expires_at": 123.0,
                "note": "proof attempt 1",
            },
            {
                "node_id": "chapter/result",
                "article_id": "af_000000000000000000000001",
                "title": "Main result",
                "owner": "path-worker",
                "claim_target": "chapter/result",
            },
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
    manifest_path = site / "publication.json"
    manifest = {
        "schema": PUBLICATION_SCHEMA,
        "complete": True,
        "source_revision": publication_source_revision(blueprint),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    class EmptyClaims:
        def list(self) -> list[dict[str, object]]:
            return []

    state = publication_bound_live_state(
        EmptyClaims(),
        blueprint_dir=blueprint,
        site_dir=site,
    )

    assert state()["source_revision"] == manifest["source_revision"]
    manifest["schema"] = "autoform-publication/v1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    wrong_schema = state()
    assert wrong_schema["claims"] == []
    assert "stale" in str(wrong_schema["error"])

    manifest["schema"] = PUBLICATION_SCHEMA
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    article.write_text("# Changed\n", encoding="utf-8")
    stale = state()
    assert stale["claims"] == []
    assert "stale" in str(stale["error"])


def test_live_overlay_builds_graph_from_the_verified_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    article = roadmap / "README.md"
    article.write_text("# Roadmap\n", encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()
    revision = publication_source_revision(blueprint)
    (site / "publication.json").write_text(
        json.dumps(
            {
                "schema": PUBLICATION_SCHEMA,
                "complete": True,
                "source_revision": revision,
            }
        ),
        encoding="utf-8",
    )

    class SnapshotClaims:
        def list(self) -> list[dict[str, object]]:
            return [
                {
                    "_key": author_claim_key("roadmap"),
                    "_malformed": False,
                    "_expired": False,
                    "owner": "worker-a",
                }
            ]

    from autoform_cli import dashboard as dashboard_module

    original = dashboard_module.load_graph_snapshot

    def mutate_after_snapshot(*args, **kwargs):
        graph = original(*args, **kwargs)
        article.write_text("# Changed after capture\n", encoding="utf-8")
        return graph

    monkeypatch.setattr(
        dashboard_module,
        "load_graph_snapshot",
        mutate_after_snapshot,
    )

    state = publication_bound_live_state(
        SnapshotClaims(),
        blueprint_dir=blueprint,
        site_dir=site,
    )()

    assert state["source_revision"] == revision
    assert state["claims"] == [
        {
            "node_id": "roadmap",
            "title": "Roadmap",
            "owner": "worker-a",
        }
    ]
    assert "error" not in state
    assert article.read_text(encoding="utf-8") == "# Changed after capture\n"


def test_live_overlay_refuses_portable_freshness_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint = tmp_path / "blueprint"
    blueprint.mkdir()
    (blueprint / "README.md").write_text("# Blueprint\n", encoding="utf-8")
    revision = publication_source_revision(blueprint)
    site = tmp_path / "site"
    site.mkdir()
    (site / "publication.json").write_text(
        json.dumps(
            {
                "schema": PUBLICATION_SCHEMA,
                "complete": True,
                "source_revision": revision,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tree_snapshot_module,
        "_DESCRIPTOR_CAPTURE_SUPPORTED",
        False,
    )

    class EmptyClaims:
        def list(self) -> list[dict[str, object]]:
            return []

    state = publication_bound_live_state(
        EmptyClaims(),
        blueprint_dir=blueprint,
        site_dir=site,
    )()

    assert state["claims"] == []
    assert "could not be captured safely" in str(state["error"])


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


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", ["/", LIVE_ENDPOINT])
def test_dashboard_rejects_dns_rebinding_host_before_serving(
    tmp_path: Path,
    path: str,
    method: str,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("private dashboard", encoding="utf-8")
    loads = []
    handler = partial(
        DashboardHandler,
        directory=str(site),
        live_state=lambda: loads.append(True) or {},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request(method, path, headers={"Host": "attacker.example"})
        response = connection.getresponse()
        response.read()
        connection.close()

        assert response.status == 421
        assert loads == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_dashboard_rejects_cross_origin_request_before_loading_claims(
    tmp_path: Path,
    method: str,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    loads = []
    handler = partial(
        DashboardHandler,
        directory=str(site),
        live_state=lambda: loads.append(True) or {},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request(
            method,
            LIVE_ENDPOINT,
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": "https://attacker.example",
            },
        )
        response = connection.getresponse()
        response.read()
        connection.close()

        assert response.status == 403
        assert loads == []
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
