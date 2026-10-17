from __future__ import annotations

import base64
import datetime
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from autoform_cli import prove2me, prove2me_cli
from autoform_cli.__main__ import main
from autoform_cli.prove2me import Client, Credentials, Prove2MeError


def _key(exp: int) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"p2m_eyJhbGciOiJIUzI1NiJ9.{claims}.c2ln"


FUTURE = int(datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc).timestamp())
KEY = _key(FUTURE)


class FakeApi:
    """A routing table standing in for prove2.me; records every request."""

    def __init__(self, routes: dict[tuple[str, str], object]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.tokens = 0
        self.reject_next_token = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1")
        if path == "/agent/refresh":
            if json.loads(request.content) != {"api_key": KEY}:
                return httpx.Response(401, json={"error": "invalid api key"})
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_at": 0, "version": "9.9"})
        if self.reject_next_token:
            self.reject_next_token = False
            return httpx.Response(401, json={"error": "token expired"})
        assert request.headers["Authorization"] == f"Bearer tok{self.tokens}"
        route = self.routes.get((request.method, path))
        if callable(route):
            route = route(request)
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)


def _client(api: FakeApi) -> Client:
    return Client(Credentials(KEY, "test"), transport=httpx.MockTransport(api))


@pytest.fixture
def cli_api(monkeypatch):
    """Route the CLI's client through a FakeApi with the key in the environment."""
    monkeypatch.setenv("PROVE2ME_API_KEY", KEY)
    holder: dict[str, FakeApi] = {}
    real = prove2me.Client

    def make(credentials, **kwargs):
        return real(credentials, transport=httpx.MockTransport(holder["api"]))

    monkeypatch.setattr(prove2me_cli.prove2me, "Client", make)
    return holder


# --- credentials -------------------------------------------------------------


def test_key_expiry_is_read_from_the_key_itself(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PROVE2ME_API_KEY", raising=False)
    (tmp_path / "credentials.json").write_text(json.dumps({"api_key": _key(1000), "username": "u"}))

    credentials = prove2me.load_credentials(tmp_path)

    assert credentials.expires_at == datetime.datetime.fromtimestamp(1000, datetime.timezone.utc)
    assert credentials.expired
    assert KEY not in repr(credentials) and "api_key" not in repr(credentials)
    with pytest.raises(Prove2MeError, match="expired"):
        Client(credentials)


def test_stored_expiry_accepts_unix_seconds_and_iso(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PROVE2ME_API_KEY", raising=False)
    for stored in (FUTURE, "2099-01-01T00:00:00Z"):
        (tmp_path / "credentials.json").write_text(json.dumps({"api_key": "opaque", "api_key_expires_at": stored}))
        assert not prove2me.load_credentials(tmp_path).expired


def test_environment_key_takes_precedence(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "credentials.json").write_text(json.dumps({"api_key": "from-file"}))
    monkeypatch.setenv("PROVE2ME_API_KEY", KEY)

    assert prove2me.load_credentials(tmp_path).source == "PROVE2ME_API_KEY"


@pytest.fixture
def default_workspace(tmp_path: Path, monkeypatch) -> Path:
    """Point the default workspace at an empty directory, never the real one."""
    default = tmp_path / "default-workspace"
    default.mkdir()
    monkeypatch.delenv("PROVE2ME_API_KEY", raising=False)
    monkeypatch.delenv("PROVE2ME_WORKSPACE", raising=False)
    monkeypatch.setattr(prove2me, "DEFAULT_WORKSPACE", default)
    return default


def test_missing_credentials_point_to_onboarding(tmp_path: Path, default_workspace: Path) -> None:
    with pytest.raises(Prove2MeError, match="start.md"):
        prove2me.load_credentials(tmp_path / "checkout")


def test_a_checkout_named_by_the_environment_still_finds_the_default_key(
    tmp_path: Path, default_workspace: Path, monkeypatch
) -> None:
    (default_workspace / "credentials.json").write_text(json.dumps({"api_key": KEY}))
    checkout = tmp_path / "prove2me-c5ea003"
    checkout.mkdir()
    monkeypatch.setenv("PROVE2ME_WORKSPACE", str(checkout))

    assert prove2me.load_credentials(prove2me.workspace_path()).api_key == KEY


def test_a_second_checkout_borrows_the_default_workspace_key(tmp_path: Path, default_workspace: Path) -> None:
    (default_workspace / "credentials.json").write_text(json.dumps({"api_key": KEY, "username": "u"}))
    checkout = tmp_path / "prove2me-c5ea003"
    checkout.mkdir()

    credentials = prove2me.load_credentials(checkout)

    assert credentials.source == str(default_workspace / "credentials.json")
    (checkout / "credentials.json").write_text(json.dumps({"api_key": _key(FUTURE + 1)}))
    assert prove2me.load_credentials(checkout).source == str(checkout / "credentials.json")


# --- client ------------------------------------------------------------------


def test_client_reauthenticates_once_on_401_and_only_talks_to_prove2me() -> None:
    api = FakeApi({("GET", "/me"): {"username": "u"}})
    client = _client(api)

    assert client.get("/me") == {"username": "u"}
    api.reject_next_token = True
    assert client.get("/me") == {"username": "u"}
    assert api.tokens == 2
    assert {request.url.host for request in api.requests} == {"prove2.me"}
    for path in ("https://evil.example/me", "//evil.example/me", "me"):
        with pytest.raises(Prove2MeError, match="relative"):
            client.get(path)


def test_errors_carry_the_server_message_but_never_the_key() -> None:
    api = FakeApi({("POST", "/verify"): httpx.Response(429, json={"error": "slow down"})})

    with pytest.raises(Prove2MeError) as error:
        _client(api).request("POST", "/verify", data={"theorem_id": "t"})

    message = str(error.value)
    assert "HTTP 429: slow down" in message and "pending" in message
    assert KEY not in message and "tok1" not in message


def test_revoked_key_is_reported_as_such() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid"})

    client = Client(Credentials(KEY, "test"), transport=httpx.MockTransport(refuse))
    with pytest.raises(Prove2MeError, match="expired or been revoked"):
        client.get("/me")


# --- missions ----------------------------------------------------------------


def _missions(request: httpx.Request) -> dict:
    offset = int(request.url.params["offset"])
    catalogue = [{"id": f"00000000-0000-0000-0000-{i:012d}", "name": f"Mission {i}"} for i in range(150)]
    catalogue[120] = {
        "id": "c36fd4df-ef29-4fbc-9bb6-1f6acf3c0733",
        "name": "Positive definite matrix integral inequality",
        "main_theorem": {"theorem_id": "root", "theorem_name": "P.main", "status": "Open"},
    }
    return {"missions": catalogue[offset : offset + 100], "total": len(catalogue)}


@pytest.mark.parametrize(
    "ref",
    [
        "https://prove2.me/missions/Positive_definite_matrix_integral_inequality/",
        "Positive_definite_matrix_integral_inequality",
        "positive definite matrix integral inequality",
        "C36FD4DF-EF29-4FBC-9BB6-1F6ACF3C0733",
    ],
)
def test_find_mission_pages_the_catalogue(ref: str) -> None:
    api = FakeApi({("GET", "/missions"): _missions})

    assert prove2me.find_mission(_client(api), ref)["name"] == "Positive definite matrix integral inequality"


def test_unknown_mission_is_an_error() -> None:
    with pytest.raises(Prove2MeError, match="no visible"):
        prove2me.find_mission(_client(FakeApi({("GET", "/missions"): _missions})), "Nope")


def test_mission_frontier_ranks_leaves_by_closability() -> None:
    api = FakeApi({
        ("GET", "/missions/m/milestones"): {
            "milestones": [{"id": "ms", "title": "One", "completed": True, "theorem": {"id": "t1"}}],
            "pagination": {"total": 1},
        },
        ("GET", "/theorems/root/open-leaves"): {
            "open_leaves": [
                {"theorem_id": "a", "theorem_name": "a", "closability": 0},
                {"theorem_id": "b", "theorem_name": "b", "closability": 3},
            ],
            "total": 2,
        },
    })
    mission = {"id": "m", "name": "M", "main_theorem": {"theorem_id": "root", "theorem_name": "r", "status": "Open"}}

    frontier = prove2me.mission_frontier(_client(api), mission)

    assert [leaf["theorem_id"] for leaf in frontier["open_leaves"]] == ["b", "a"]
    assert frontier["milestones"][0]["completed"] is True


# --- mirroring ---------------------------------------------------------------


TARGET = {
    "theorem_id": "leaf",
    "theorem_name": "P01.leaf",
    "status": "Open",
    "mathlib_rev": "c5ea",
    "preamble": "import Definitions.Def_outer\nimport Theorems.Thm_P01_helper",
    "formal_statement": "theorem P01.leaf : True := by\n  sorry",
}


def _definitions(request: httpx.Request) -> dict:
    params = request.url.params
    assert params["env"] == "c5ea"
    if params.get("status") == "Definition":
        bodies = {"outer": "import Definitions.Def_inner\ndef outer := inner", "inner": "def inner := 1"}
        name = params["theorem_name"]
        return {"theorems": [{"theorem_name": name, "definition": bodies[name]}] if name in bodies else []}
    if params.get("theorem_name") == "P01_helper":
        return {"theorems": []}
    return {"theorems": [{"theorem_id": "h", "theorem_name": "P01.helper", "status": "Proved"}]}


def test_fetch_mirrors_the_target_and_its_platform_imports(tmp_path: Path) -> None:
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "mathlib", "rev": "c5ea"}]}))
    api = FakeApi({
        ("GET", "/theorems/leaf"): TARGET,
        ("GET", "/theorems/h"): {
            "theorem_name": "P01.helper",
            "formal_statement": "theorem P01.helper : True := sorry",
        },
        ("GET", "/theorems"): _definitions,
    })

    result = prove2me.fetch_theorem(_client(api), "leaf", tmp_path)

    assert result.environment_matches and not result.unresolved
    assert sorted(result.written) == [
        "Definitions/Def_inner.lean",
        "Definitions/Def_outer.lean",
        "Theorems/Thm_P01_helper.lean",
        "Theorems/Thm_P01_leaf.lean",
    ]
    assert (tmp_path / "Theorems/Thm_P01_leaf.lean").read_text() == (
        TARGET["preamble"] + "\n\n" + TARGET["formal_statement"] + "\n"
    )
    assert prove2me.fetch_theorem(_client(api), "leaf", tmp_path).written == []


def test_fetch_reports_an_environment_mismatch(tmp_path: Path) -> None:
    (tmp_path / "lakefile.lean").write_text('require mathlib from git\n  "https://x/mathlib4.git" @\n  "777aaa6"\n')
    api = FakeApi({("GET", "/theorems/leaf"): {**TARGET, "preamble": ""}})

    result = prove2me.fetch_theorem(_client(api), "leaf", tmp_path)

    assert result.workspace_rev == "777aaa6"
    assert not result.environment_matches


# --- local gates -------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("theorem solution : True := trivial", []),
        ("-- sorry in a comment\n/- and sorry /- nested -/ here -/\ntheorem solution : True := trivial", []),
        ('theorem solution : "sorry".length = 5 := rfl', []),
        ("namespace X\nend X\nnoncomputable section\ntheorem solution : True := trivial\nend", []),
        ("theorem solution : True := by sorry", ["sorry"]),
        ("theorem solution : True := by admit", ["sorry"]),
        ("import Theorems.Thm_P01_leaf\ntheorem solution : True := trivial", ["own target"]),
        ("namespace P01\ntheorem solution : True := trivial\nend P01", ["inside a namespace"]),
        ("theorem solution2 : True := trivial", ["no top-level"]),
    ],
)
def test_check_solution_enforces_the_platform_gates(source: str, expected: list[str]) -> None:
    problems = prove2me.check_solution(source, "P01.leaf")

    assert len(problems) == len(expected)
    for problem, fragment in zip(problems, expected):
        assert fragment in problem


def test_compile_requires_the_solution_inside_the_workspace(tmp_path: Path) -> None:
    ok, message = prove2me.compile_locally(tmp_path / "elsewhere.lean", tmp_path / "ws", "P01.leaf", 10)

    assert not ok and "Solutions" in message


# --- verdicts ----------------------------------------------------------------


def test_wait_for_verdict_backs_off_until_a_final_status() -> None:
    statuses = iter(["PENDING", "PENDING", "PENDING", "ACCEPTED"])
    api = FakeApi({("GET", "/verify"): lambda request: {"status": next(statuses), "error_message": ""}})
    sleeps: list[float] = []

    verdict = prove2me.wait_for_verdict(_client(api), "s", sleep=sleeps.append, clock=lambda: 0.0)

    assert verdict["status"] == "ACCEPTED"
    assert sleeps == [5.0, 10.0, 20.0]


def test_wait_for_verdict_gives_up_at_the_deadline() -> None:
    api = FakeApi({("GET", "/verify"): {"status": "PENDING"}})
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    verdict = prove2me.wait_for_verdict(_client(api), "s", timeout=12, sleep=sleep, clock=lambda: now[0])

    assert verdict["status"] == "PENDING"
    assert now[0] <= 12


# --- command line ------------------------------------------------------------


def test_auth_command_never_prints_the_key(cli_api, capsys) -> None:
    cli_api["api"] = FakeApi({})

    assert main(["prove2me", "auth"]) == 0
    output = capsys.readouterr()
    assert "platform version 9.9" in output.out
    assert KEY not in output.out + output.err


def test_dry_run_reports_gates_and_never_submits(cli_api, tmp_path: Path, capsys) -> None:
    api = FakeApi({("GET", "/theorems/leaf"): TARGET})
    cli_api["api"] = api
    bad = tmp_path / "bad.lean"
    bad.write_text("theorem solution : True := by sorry\n")
    good = tmp_path / "good.lean"
    good.write_text("theorem solution : True := trivial\n")

    flags = ["--dry-run", "--skip-compile", "--workspace", str(tmp_path)]
    assert main(["prove2me", "verify", "leaf", str(bad), *flags]) == 1
    assert "sorry" in capsys.readouterr().out
    assert main(["prove2me", "verify", "leaf", str(good), *flags]) == 0
    assert "nothing submitted" in capsys.readouterr().out
    assert not any(request.method != "GET" for request in api.requests if request.url.path != "/api/v1/agent/refresh")


def test_verify_refuses_a_mismatched_environment_before_compiling(cli_api, tmp_path: Path, capsys, monkeypatch) -> None:
    cli_api["api"] = FakeApi({("GET", "/theorems/leaf"): TARGET})
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": "mathlib", "rev": "777a"}]}))
    good = tmp_path / "good.lean"
    good.write_text("theorem solution : True := trivial\n")
    monkeypatch.setattr(prove2me, "compile_locally", lambda *a, **k: pytest.fail("compiled despite mismatch"))

    assert main(["prove2me", "verify", "leaf", str(good), "--workspace", str(tmp_path)]) == 1
    assert "re-pin" in capsys.readouterr().out


def test_verify_submits_with_explanation_and_waits(cli_api, tmp_path: Path, capsys, monkeypatch) -> None:
    submitted: list[httpx.Request] = []

    def accept(request: httpx.Request) -> dict:
        submitted.append(request)
        return {"submission_id": "sub-1", "status": "PENDING"}

    cli_api["api"] = FakeApi({
        ("GET", "/theorems/leaf"): TARGET,
        ("POST", "/verify"): accept,
        ("GET", "/verify"): {"status": "ACCEPTED", "error_message": ""},
    })
    solution = tmp_path / "Sol.lean"
    solution.write_text("theorem solution : True := trivial\n")
    explanation = tmp_path / "why.md"
    explanation.write_text("We prove $\\top$ directly.")

    code = main([
        "prove2me", "verify", "leaf", str(solution), "--skip-compile",
        "--explanation-file", str(explanation), "--workspace", str(tmp_path), "--json",
    ])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["submission_id"] == "sub-1" and report["status"] == "ACCEPTED"
    body = submitted[0].content
    assert b'name="theorem_id"\r\n\r\nleaf' in body
    assert b'name="proof_type"\r\n\r\nprove' in body
    assert b"We prove $\\top$ directly." in body
    assert b"theorem solution : True := trivial" in body


# --- publishing --------------------------------------------------------------


CHILD = {
    "theorem_name": "P01.child",
    "theorem_title": "Child",
    "formal_statement": "namespace P01\ntheorem child : True := by\n  sorry\nend P01",
    "natural_language_statement": "Truth holds.",
    "preamble": "import Definitions.Def_outer",
}


@pytest.mark.parametrize(
    ("item", "fragment"),
    [
        ({**CHILD, "natural_language_statement": ""}, "missing natural_language_statement"),
        ({**CHILD, "formal_statement": "theorem P01.child : True := trivial"}, "must end with"),
        ({**CHILD, "formal_statement": "theorem other : True := by sorry"}, "does not declare"),
        ({**CHILD, "theorem_title": "x" * 201}, "200 characters"),
        ({**CHILD, "private": True}, "--private"),
    ],
)
def test_check_publish_rejects_malformed_problems(item: dict, fragment: str) -> None:
    assert prove2me.check_publish("problem", CHILD) == []
    assert any(fragment in problem for problem in prove2me.check_publish("problem", item))


def test_check_publish_rejects_sorry_in_definitions() -> None:
    definition = {"definition_name": "helper", "definition_title": "Helper", "definition": "def helper : Nat := sorry"}

    assert any("sorry-free" in problem for problem in prove2me.check_publish("definition", definition))
    renamed = {**definition, "definition_name": "a.b"}
    assert any("must match" in p for p in prove2me.check_publish("definition", renamed))


def test_publish_sends_the_parent_environment_and_waits_for_each_job(
    cli_api, tmp_path: Path, capsys, monkeypatch
) -> None:
    bodies: list[dict] = []
    states = iter(["COMPILING", "PUBLISHED"])

    def queue(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"jobs": [{"job_id": "j1", "name": "P01.child"}], "errors": []})

    cli_api["api"] = FakeApi({
        ("POST", "/submit-problem"): queue,
        ("GET", "/publish-jobs/j1"): lambda request: {"status": next(states), "theorem_id": "new-id"},
    })
    monkeypatch.setattr(prove2me.time, "sleep", lambda seconds: None)
    spec = tmp_path / "child.json"
    spec.write_text(json.dumps({"problems": [CHILD]}))

    code = main(["prove2me", "publish", "problem", str(spec), "--env", "c5ea", "--private",
                 "--workspace", str(tmp_path)])

    assert code == 0
    assert bodies == [{"problems": [CHILD], "env": "c5ea", "private": True}]
    assert "P01.child: PUBLISHED job j1 -> theorem new-id" in capsys.readouterr().out


def test_publish_dry_run_and_invalid_specs_never_post(cli_api, tmp_path: Path, capsys) -> None:
    api = FakeApi({})
    cli_api["api"] = api
    good = tmp_path / "good.json"
    good.write_text(json.dumps(CHILD))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**CHILD, "formal_statement": "theorem P01.child : True := trivial"}))

    assert main(["prove2me", "publish", "problem", str(good), "--env", "c5ea", "--dry-run",
                 "--workspace", str(tmp_path)]) == 0
    assert main(["prove2me", "publish", "problem", str(bad), "--env", "c5ea", "--workspace", str(tmp_path)]) == 1
    assert "must end with" in capsys.readouterr().out
    assert not any(request.method == "POST" and "submit" in request.url.path for request in api.requests)


# --- mission proposals -------------------------------------------------------


DEF_ITEM = {
    "kind": "definition",
    "definition_name": "P01_helper",
    "definition_title": "Helper",
    "definition": "import Mathlib\n\ndef P01.helper : Nat := 6",
    "readback": "The natural number $6$.",
    "readback_model": "m",
}
GOAL_ITEM = {
    "kind": "theorem",
    **CHILD,
    "theorem_name": "P01.goal",
    "formal_statement": "namespace P01\ntheorem goal : ¬ Nat.Prime helper := by sorry\nend P01",
    "preamble": "import Definitions.Def_P01_helper",
    "readback": "Six is not prime.",
    "readback_model": "m",
    "goal": True,
}
STEP_ITEM = {
    "kind": "theorem",
    **CHILD,
    "readback": "True holds.",
    "readback_model": "m",
    "milestone": {"title": "Lemma 1 - truth", "description": "Truth holds."},
}
PROPOSAL = {
    "proposal": {"name": "Six", "description": "## Motivation\n...", "mission_type": "OpenProblem",
                 "field_ids": ["f1"]},
    "items": [DEF_ITEM, STEP_ITEM, GOAL_ITEM],
}


def _with(items: list[dict] | None = None, **meta: object) -> dict:
    return {"proposal": {**PROPOSAL["proposal"], **meta}, "items": items if items is not None else PROPOSAL["items"]}


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ({"items": []}, '{"proposal"'),
        (_with(mission_type="Conjecture"), "mission_type"),
        (_with(description=""), "missing description"),
        (_with(campaign="x"), "unknown field"),
        (_with([DEF_ITEM, STEP_ITEM, {**GOAL_ITEM, "goal": False}]), "exactly one"),
        (_with([GOAL_ITEM, DEF_ITEM]), "listed after it"),
        (_with([DEF_ITEM, {**GOAL_ITEM, "milestone": {"title": "t", "description": "d"}}]), "only a non-goal"),
        (_with([DEF_ITEM, {**GOAL_ITEM, "env": "c5ea"}]), "on the proposal"),
        (_with([DEF_ITEM, {**GOAL_ITEM, "readback_model": ""}]), "readback_model"),
        (_with([DEF_ITEM, {**GOAL_ITEM, "formal_statement": "theorem P01.goal : True := trivial"}]), "must end with"),
        (_with([{"kind": "reference", "theorem_id": "t", "goal": True, "preamble": "x"}]), "only theorem_id"),
    ],
)
def test_check_proposal_rejects_malformed_specs(spec: dict, fragment: str) -> None:
    assert prove2me.check_proposal(PROPOSAL) == ([], [])
    problems, _ = prove2me.check_proposal(spec)
    assert any(fragment in problem for problem in problems), problems


def test_check_proposal_warns_about_what_blocks_submission_later() -> None:
    bare = {key: value for key, value in GOAL_ITEM.items() if key not in ("readback", "readback_model")}
    problems, warnings = prove2me.check_proposal(_with([DEF_ITEM, bare], field_ids=[]))

    assert problems == []
    assert any("field_ids" in warning for warning in warnings)
    assert any("P01.goal: no read-back" in warning for warning in warnings)


def test_compile_proposal_writes_drafts_where_imports_resolve_and_never_clobbers(tmp_path: Path, monkeypatch) -> None:
    builds: list[list[str]] = []
    monkeypatch.setattr(
        prove2me.subprocess,
        "run",
        lambda command, **kwargs: builds.append(command) or subprocess.CompletedProcess(command, 0, "", ""),
    )

    ok, _ = prove2me.compile_proposal(PROPOSAL["items"], tmp_path, 60)

    assert ok
    assert builds == [
        ["lake", "build", "Definitions.Def_P01_helper", "Theorems.Thm_P01_child", "Theorems.Thm_P01_goal"]
    ]
    goal = (tmp_path / "Theorems" / "Thm_P01_goal.lean").read_text()
    assert goal.startswith(prove2me.DRAFT_MARKER + "import Definitions.Def_P01_helper\n\nnamespace P01")
    assert prove2me.compile_proposal(PROPOSAL["items"], tmp_path, 60)[0]  # an earlier draft is rewritten

    (tmp_path / "Theorems" / "Thm_P01_goal.lean").write_text("theorem P01.goal : True := by sorry\n")
    ok, output = prove2me.compile_proposal(PROPOSAL["items"], tmp_path, 60)
    assert not ok and "not a local draft" in output
    assert (tmp_path / "Theorems" / "Thm_P01_goal.lean").read_text() == "theorem P01.goal : True := by sorry\n"


def _proposal_api(state: dict) -> FakeApi:
    """A FakeApi that keeps one proposal in `state` and records write bodies."""
    state.setdefault("writes", [])

    def create(request: httpx.Request) -> httpx.Response:
        state["writes"].append(("POST /mission-proposals", json.loads(request.content)))
        state["proposal"] = {"id": "prop", "status": "Draft", "visibility": "public", "env": "0df444a",
                             "items": [], **json.loads(request.content)}
        return httpx.Response(201, json=state["proposal"])

    def add_item(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        state["writes"].append(("POST items", body))
        item = {"id": f"i{len(state['proposal']['items'])}", **body}
        state["proposal"]["items"].append(item)
        return httpx.Response(201, json=item)

    def record(label: str):
        def handle(request: httpx.Request) -> dict:
            state["writes"].append((label, json.loads(request.content)))
            return {}
        return handle

    routes = {
        ("POST", "/mission-proposals"): create,
        ("GET", "/mission-proposals/prop"): lambda request: state["proposal"],
        ("PATCH", "/mission-proposals/prop"): record("PATCH proposal"),
        ("POST", "/mission-proposals/prop/items"): add_item,
        ("POST", "/mission-proposals/prop/milestones"): record("POST milestone"),
        ("GET", "/environments"): ENVIRONMENTS,
    }
    for index in range(3):
        routes[("PATCH", f"/mission-proposals/prop/items/i{index}")] = record(f"PATCH i{index}")
    return FakeApi(routes)


def test_propose_creates_a_draft_with_goal_order_and_milestones(cli_api, tmp_path: Path, capsys) -> None:
    state: dict = {}
    cli_api["api"] = _proposal_api(state)
    spec = tmp_path / "proposal.json"
    spec.write_text(json.dumps(PROPOSAL))

    code = main(["prove2me", "propose", str(spec), "--skip-compile", "--workspace", str(tmp_path), "--json"])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["proposal_id"] == "prop" and report["status"] == "Draft"
    writes = state["writes"]
    assert writes[0] == ("POST /mission-proposals", PROPOSAL["proposal"])
    sent = [body for label, body in writes if label == "POST items"]
    assert [body.get("theorem_name") or body.get("definition_name") for body in sent] == [
        "P01_helper", "P01.child", "P01.goal"]
    assert all("goal" not in body and "milestone" not in body for body in sent)
    assert ("PATCH proposal", {"main_item_id": "i2", "item_order": ["i0", "i1", "i2"]}) in writes
    assert ("POST milestone", {"item_id": "i1", "milestone_title": "Lemma 1 - truth",
                               "milestone_description": "Truth holds."}) in writes
    assert not any("submit" in request.url.path for request in cli_api["api"].requests)


def test_propose_updates_an_existing_draft_in_place(cli_api, tmp_path: Path, capsys) -> None:
    state: dict = {"proposal": {"id": "prop", "status": "Draft", "env": "0df444a", "items": [
        {"id": "i0", "kind": "definition", "definition_name": "P01_helper"},
        {"id": "i1", "kind": "theorem", "theorem_name": "P01.goal"},
        {"id": "i2", "kind": "theorem", "theorem_name": "P01.dropped"},
    ]}}
    cli_api["api"] = _proposal_api(state)
    spec = tmp_path / "proposal.json"
    spec.write_text(json.dumps(_with([DEF_ITEM, GOAL_ITEM])))

    code = main(["prove2me", "propose", str(spec), "--proposal", "prop", "--skip-compile",
                 "--workspace", str(tmp_path)])

    assert code == 0
    labels = [label for label, _ in state["writes"]]
    assert "POST items" not in labels and "POST /mission-proposals" not in labels
    assert {"PATCH i0", "PATCH i1"} <= set(labels)
    assert ("PATCH proposal", {"main_item_id": "i1", "item_order": ["i0", "i1", "i2"]}) in state["writes"]
    assert "P01.dropped is in the proposal but not in the spec" in capsys.readouterr().out


def test_propose_refuses_a_submitted_proposal(cli_api, tmp_path: Path, capsys) -> None:
    state: dict = {"proposal": {"id": "prop", "status": "In review", "items": []}}
    cli_api["api"] = _proposal_api(state)
    spec = tmp_path / "proposal.json"
    spec.write_text(json.dumps(PROPOSAL))

    assert main(["prove2me", "propose", str(spec), "--proposal", "prop", "--skip-compile",
                 "--workspace", str(tmp_path)]) == 1
    assert "can no longer be edited" in capsys.readouterr().err
    assert state["writes"] == []


def test_propose_dry_run_and_env_mismatch_never_upload(cli_api, tmp_path: Path, capsys, monkeypatch) -> None:
    state: dict = {}
    cli_api["api"] = _proposal_api(state)
    spec = tmp_path / "proposal.json"
    spec.write_text(json.dumps(PROPOSAL))
    monkeypatch.setattr(prove2me, "compile_proposal", lambda *a, **k: pytest.fail("compiled despite mismatch"))
    manifest = {"packages": [{"name": "mathlib", "rev": C5EA["mathlib_rev"]}]}
    (tmp_path / "lake-manifest.json").write_text(json.dumps(manifest))

    assert main(["prove2me", "propose", str(spec), "--workspace", str(tmp_path)]) == 1
    assert "setup --env 0df444a" in capsys.readouterr().out
    assert main(["prove2me", "propose", str(spec), "--dry-run", "--skip-compile", "--workspace", str(tmp_path)]) == 0
    assert "nothing uploaded" in capsys.readouterr().out
    assert state["writes"] == []


# --- setup -------------------------------------------------------------------


ENVIRONMENTS = {
    "environments": [
        {"mathlib_rev": "0df444a" + "0" * 33, "toolchain": "leanprover/lean4:v4.33.1", "is_default": True},
        {"mathlib_rev": "c5ea003" + "0" * 33, "toolchain": "leanprover/lean4:v4.30.0", "is_default": False},
    ]
}
C5EA = ENVIRONMENTS["environments"][1]


def test_store_api_key_keeps_other_fields_and_is_private(tmp_path: Path) -> None:
    (tmp_path / "credentials.json").write_text(json.dumps({"api_key": "old", "username": "u"}))

    path = prove2me.store_api_key(tmp_path, KEY)

    assert json.loads(path.read_text()) == {"api_key": KEY, "username": "u", "api_key_expires_at": FUTURE}
    assert path.stat().st_mode & 0o777 == 0o600


def test_choose_workspace_uses_one_checkout_per_environment(tmp_path: Path) -> None:
    home = tmp_path / "prove2me_workspace"
    home.mkdir()
    assert prove2me.choose_workspace(home, C5EA["mathlib_rev"]) == home

    (home / "lakefile.lean").write_text(prove2me._LAKEFILE.format(rev=C5EA["mathlib_rev"]))
    assert prove2me.choose_workspace(home, C5EA["mathlib_rev"]) == home
    assert prove2me.choose_workspace(home, "0df444a" + "0" * 33) == tmp_path / "prove2me-0df444a"


def test_pin_environment_writes_missing_files_and_never_repins(tmp_path: Path) -> None:
    assert prove2me.pin_environment(tmp_path, C5EA) == ["lean-toolchain", "lakefile.lean"]
    assert (tmp_path / "lean-toolchain").read_text() == "leanprover/lean4:v4.30.0\n"
    assert prove2me.workspace_mathlib_rev(tmp_path) == C5EA["mathlib_rev"]
    assert prove2me.pin_environment(tmp_path, C5EA) == []

    with pytest.raises(Prove2MeError, match="pinned to Mathlib c5ea003"):
        prove2me.pin_environment(tmp_path, ENVIRONMENTS["environments"][0])


def _route_setup_client(monkeypatch, api: FakeApi) -> None:
    real = prove2me.Client
    monkeypatch.setattr(prove2me_cli.prove2me, "Client",
                        lambda credentials, **kwargs: real(credentials, transport=httpx.MockTransport(api)))


def test_setup_asks_for_a_key_only_in_a_terminal_and_saves_a_valid_one(
    default_workspace: Path, monkeypatch, capsys
) -> None:
    _route_setup_client(monkeypatch, FakeApi({}))
    monkeypatch.setattr(prove2me_cli.sys.stdin, "isatty", lambda: True)
    entered = iter(["", "p2m_not.a.key", KEY])
    monkeypatch.setattr(prove2me_cli.getpass, "getpass", lambda prompt: next(entered))

    assert main(["prove2me", "setup"]) == 0

    stored = json.loads((default_workspace / "credentials.json").read_text())
    assert stored["api_key"] == KEY
    output = capsys.readouterr()
    assert "rejected" in output.err and "saved the key" in output.err
    assert KEY not in output.out + output.err


def test_setup_never_prompts_without_a_terminal(default_workspace: Path, monkeypatch, capsys) -> None:
    _route_setup_client(monkeypatch, FakeApi({}))
    monkeypatch.setattr(prove2me_cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(prove2me_cli.getpass, "getpass", lambda prompt: pytest.fail("prompted"))

    assert main(["prove2me", "setup"]) == 1
    assert "in a terminal" in capsys.readouterr().err


def test_setup_for_a_mission_pins_a_checkout_for_its_environment(
    default_workspace: Path, monkeypatch, capsys
) -> None:
    (default_workspace / "credentials.json").write_text(json.dumps({"api_key": KEY}))
    (default_workspace / "lakefile.lean").write_text(prove2me._LAKEFILE.format(rev="777aaa6" + "0" * 33))
    second = default_workspace.parent / "prove2me-c5ea003"
    second.mkdir()
    _route_setup_client(monkeypatch, FakeApi({
        ("GET", "/missions"): {"missions": [{"id": "m", "name": "Weak Goldbach Conjecture",
                                             "main_theorem": {"theorem_id": "goal"}}], "total": 1},
        ("GET", "/theorems/goal"): {"theorem_id": "goal", "mathlib_rev": C5EA["mathlib_rev"]},
        ("GET", "/environments"): ENVIRONMENTS,
    }))

    code = main(["prove2me", "setup", "--mission", "Weak_Goldbach_Conjecture", "--no-fetch", "--print-workspace"])

    assert code == 0
    output = capsys.readouterr()
    assert output.out.strip() == str(second)
    assert prove2me.workspace_mathlib_rev(second) == C5EA["mathlib_rev"]
    assert (second / "lean-toolchain").read_text().strip() == "leanprover/lean4:v4.30.0"
    assert prove2me.workspace_mathlib_rev(default_workspace).startswith("777aaa6")


def test_a_checkout_set_up_from_a_relative_home_still_finds_the_key(
    tmp_path: Path, default_workspace: Path, monkeypatch, capsys
) -> None:
    # formalize_statement.sh exports PROVE2ME_WORKSPACE as the checkout and then
    # changes directory, so neither the relative home nor the env var reaches the key.
    home = tmp_path / "volume" / "prove2me_workspace"
    home.mkdir(parents=True)
    (home / "credentials.json").write_text(json.dumps({"api_key": KEY}))
    (home / "lakefile.lean").write_text(prove2me._LAKEFILE.format(rev="777aaa6" + "0" * 33))
    (tmp_path / "volume" / "prove2me-c5ea003").mkdir()
    monkeypatch.chdir(tmp_path / "volume")
    monkeypatch.setenv("PROVE2ME_WORKSPACE", "prove2me_workspace")
    _route_setup_client(monkeypatch, _mission_api(C5EA["mathlib_rev"]))

    assert main(["prove2me", "setup", "--mission", "Weak_Goldbach_Conjecture", "--no-fetch", "--print-workspace"]) == 0
    checkout = Path(capsys.readouterr().out.strip())

    assert checkout == tmp_path / "volume" / "prove2me-c5ea003"
    assert (checkout / "credentials.json").resolve() == (home / "credentials.json").resolve()
    monkeypatch.setenv("PROVE2ME_WORKSPACE", str(checkout))
    monkeypatch.chdir(tmp_path)
    assert prove2me.load_credentials(prove2me.workspace_path()).api_key == KEY
    assert prove2me.share_credentials(checkout, home) is False  # already linked


def _mission_api(rev: str) -> FakeApi:
    return FakeApi({
        ("GET", "/missions"): {"missions": [{"id": "m", "name": "Weak Goldbach Conjecture",
                                             "main_theorem": {"theorem_id": "goal"}}], "total": 1},
        ("GET", "/theorems/goal"): {"theorem_id": "goal", "mathlib_rev": rev},
        ("GET", "/environments"): ENVIRONMENTS,
    })


def test_setup_refuses_to_clone_or_download_without_a_yes(default_workspace: Path, monkeypatch, capsys) -> None:
    (default_workspace / "credentials.json").write_text(json.dumps({"api_key": KEY}))
    (default_workspace / "lakefile.lean").write_text(prove2me._LAKEFILE.format(rev="777aaa6" + "0" * 33))
    _route_setup_client(monkeypatch, _mission_api(C5EA["mathlib_rev"]))
    monkeypatch.setattr(prove2me, "clone_workspace", lambda path: pytest.fail("cloned"))
    monkeypatch.setattr(prove2me_cli.sys.stdin, "isatty", lambda: False)

    assert main(["prove2me", "setup", "--mission", "Weak_Goldbach_Conjecture", "--no-fetch"]) == 1
    output = capsys.readouterr().err
    assert "clone the Prove2Me workspace into" in output and "pass --yes" in output
    assert not (default_workspace.parent / "prove2me-c5ea003").exists()


def test_setup_checks_free_space_before_downloading_mathlib(default_workspace: Path, monkeypatch, capsys) -> None:
    (default_workspace / "credentials.json").write_text(json.dumps({"api_key": KEY}))
    _route_setup_client(monkeypatch, _mission_api(C5EA["mathlib_rev"]))
    monkeypatch.setattr(prove2me_cli.shutil, "disk_usage", lambda path: shutil_usage(free=1 * 1024**3))
    monkeypatch.setattr(prove2me_cli.subprocess, "run", lambda *a, **k: pytest.fail("ran lake"))

    assert main(["prove2me", "setup", "--mission", "Weak_Goldbach_Conjecture", "--yes"]) == 1
    assert "only 1.0 GB is free" in capsys.readouterr().err


def shutil_usage(free: int):
    import shutil

    return shutil._ntuple_diskusage(total=100 * 1024**3, used=0, free=free)
