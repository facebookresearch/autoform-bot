from __future__ import annotations

import http.client
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from autoform_cli.ui.dashboard import DashboardHandler, serve_dashboard


def test_dashboard_handler_serves_only_the_static_site(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("dashboard", encoding="utf-8")
    handler = partial(DashboardHandler, directory=str(site))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/", timeout=5) as response:  # noqa: S310
            assert response.read() == b"dashboard"
        # The claim overlay's endpoint went in 0.8.0; the site has no such file.
        with pytest.raises(HTTPError) as missing:
            urlopen(f"http://{host}:{port}/__autoform/live.json", timeout=5)  # noqa: S310
        assert missing.value.code == 404
        missing.value.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_dashboard_rejects_dns_rebinding_host_before_serving(tmp_path: Path, method: str) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("private dashboard", encoding="utf-8")
    handler = partial(DashboardHandler, directory=str(site))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request(method, "/", headers={"Host": "attacker.example"})
        response = connection.getresponse()
        body = response.read()
        connection.close()

        assert response.status == 421
        assert b"private dashboard" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_dashboard_rejects_cross_origin_request_before_serving(tmp_path: Path, method: str) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("private dashboard", encoding="utf-8")
    handler = partial(DashboardHandler, directory=str(site))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request(
            method,
            "/",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": "https://attacker.example",
            },
        )
        response = connection.getresponse()
        body = response.read()
        connection.close()

        assert response.status == 403
        assert b"private dashboard" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_refuses_non_loopback_or_missing_site(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        serve_dashboard(tmp_path, host="0.0.0.0")
    with pytest.raises(ValueError, match="does not exist"):
        serve_dashboard(tmp_path / "missing")
    # Render marks a finished site with publication.json; any other directory, such as a checkout, is refused.
    with pytest.raises(ValueError, match="built site has no publication.json"):
        serve_dashboard(tmp_path)


def test_localhost_is_bound_as_literal_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli.ui import dashboard

    site = tmp_path / "site"
    site.mkdir()
    (site / "publication.json").write_text("{}", encoding="utf-8")
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
        host="localhost",
        on_ready=lambda host, port: observed.update(ready=(host, port)),
    )

    assert observed["address"] == ("127.0.0.1", 0)
    assert observed["ready"] == ("127.0.0.1", 43210)
    assert observed["served"] is True


def test_dashboard_serves_render_output_and_the_site_mkdocs_built_from_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    from autoform_cli.scaffold import scaffold_project
    from autoform_cli.ui import dashboard
    from autoform_cli.ui.render import render_site

    project = tmp_path / "project"
    scaffold_project(project, title="Dashboard fixture")
    (project / "blueprint/roadmap/README.md").write_text(
        "# Dashboard fixture\n\nNo mathematical articles are authored yet.\n", encoding="utf-8"
    )
    render_site(project / "blueprint", project / "site-src")
    build(load_config(config_file=str(project / "mkdocs.yml"), strict=True))
    served: list[str] = []

    class FakeServer:
        server_address = ("127.0.0.1", 43210)

        def __init__(self, address, handler) -> None:
            served.append(handler.keywords["directory"])

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def serve_forever(self) -> None:
            return None

    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", FakeServer)
    # Render writes publication.json beside its pages and MkDocs copies it, so both
    # directories that once held the overlay's script are still served.
    for site in ("site-src", "site"):
        dashboard.serve_dashboard(project / site)

    assert served == [str((project / "site-src").resolve()), str((project / "site").resolve())]


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

    def serve(site_dir: Path, *, host: str, port: int, on_ready) -> None:
        observed.update(site=site_dir, host=host, port=port)
        on_ready(host, 43210)

    monkeypatch.setattr("autoform_cli.ui.dashboard.serve_dashboard", serve)
    # The dashboard reads no claim board, so it needs no origin remote.
    monkeypatch.setattr(cli, "run_git", lambda *arguments, **kwargs: pytest.fail("the dashboard ran Git"))

    assert cli.main(["dashboard", str(project), "--site-dir", "built-site"]) == 0
    assert observed == {"site": site, "host": "127.0.0.1", "port": 0}
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("Dashboard: http://127.0.0.1:43210/\n", "")


def test_dashboard_cli_reports_a_target_it_cannot_resolve(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autoform_cli import __main__ as cli

    assert cli.main(["dashboard", str(tmp_path / "missing")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")


def test_dashboard_cli_has_no_claim_board_options(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from autoform_cli import __main__ as cli

    # 0.8.0 removed --repo and --scratch with the claim overlay, so each is an unknown option.
    for flag, value in (("--repo", str(tmp_path / "claims.git")), ("--scratch", str(tmp_path / "scratch"))):
        with pytest.raises(SystemExit) as refused:
            cli.main(["dashboard", str(tmp_path), flag, value])
        assert refused.value.code == 2 and f"unrecognized arguments: {flag}" in capsys.readouterr().err
