from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from autoform_cli.ui.dashboard import DashboardHandler, serve_dashboard
from tests.unexaminable import unexaminable


@pytest.fixture
def dashboard(tmp_path: Path) -> Iterator[tuple[str, int]]:
    """A dashboard serving a site whose page says "private dashboard", beside a file outside the site."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("private dashboard", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside the site", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(DashboardHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(address: tuple[str, int], method: str, path: str, headers: dict[str, str]) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection(*address, timeout=5)
    try:
        connection.putrequest(method, path, skip_host=True)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(("headers", "status"), [
    ({"Host": "127.0.0.1:{port}"}, 200),
    ({"Host": "localhost:{port}"}, 200),
    ({"Host": "localhost:{port}", "Origin": "http://localhost:{port}"}, 200),
    # DNS rebinding: a page on another name, resolved to the loopback address, reads nothing.
    ({"Host": "attacker.example"}, 421),
    ({"Host": "127.0.0.1:{other}"}, 421),
    ({"Host": "user@127.0.0.1:{port}"}, 421),
    ({}, 421),
    # A page elsewhere may send a request here, but it is refused before anything is served.
    ({"Host": "127.0.0.1:{port}", "Origin": "https://attacker.example"}, 403),
    ({"Host": "127.0.0.1:{port}", "Origin": "https://127.0.0.1:{port}"}, 403),
], ids=["loopback", "localhost", "same-origin", "rebound-host", "other-port", "userinfo", "no-host",
        "cross-origin", "https-origin"])
def test_dashboard_answers_only_its_own_loopback_authority(
    dashboard: tuple[str, int], method: str, headers: dict[str, str], status: int,
) -> None:
    port = dashboard[1]
    sent = {name: value.format(port=port, other=port + 1) for name, value in headers.items()}
    answered, body = _request(dashboard, method, "/", sent)
    assert answered == status
    assert (b"private dashboard" in body) == (status == 200 and method == "GET")


def test_dashboard_serves_the_site_and_nothing_outside_it(dashboard: tuple[str, int]) -> None:
    host = {"Host": f"127.0.0.1:{dashboard[1]}"}
    assert _request(dashboard, "GET", "/index.html", host) == (200, b"private dashboard")
    for path in ("/../outside.txt", "/%2e%2e/outside.txt", "/__autoform/live.json"):
        status, body = _request(dashboard, "GET", path, host)
        assert status == 404 and b"outside the site" not in body, path


def test_dashboard_refuses_non_loopback_or_missing_site(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        serve_dashboard(tmp_path, host="0.0.0.0")
    with pytest.raises(ValueError, match="does not exist"):
        serve_dashboard(tmp_path / "missing")
    # Render marks a finished site with publication.json; any other directory, such as a checkout, is refused.
    with pytest.raises(ValueError, match="built site has no publication.json"):
        serve_dashboard(tmp_path)


@pytest.mark.parametrize(
    ("examined", "follow_symlinks"),
    [("", None), ("", True), ("", False), ("publication.json", None)],
    ids=["the site", "the site, followed", "the site, not followed", "its publication.json"],
)
def test_dashboard_calls_a_site_it_cannot_examine_unreadable_on_every_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, examined: str, follow_symlinks: bool | None
) -> None:
    # From Python 3.14, pathlib's checks give False for a path they cannot examine, which said the site did not
    # exist or had no publication.json. Before 3.14 they raised, which `dashboard` prints, as it does again.
    (tmp_path / "publication.json").write_text("{}", encoding="utf-8")
    unexaminable(monkeypatch, tmp_path / examined, follow_symlinks=follow_symlinks)

    def served(host: str, port: int) -> None:
        raise AssertionError("served a site it could not examine")

    with pytest.raises(PermissionError):
        serve_dashboard(tmp_path, on_ready=served)


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
