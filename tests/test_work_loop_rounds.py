"""End-to-end rounds of `autoform work run` against local Git remotes, a fake `gh` and a fake agent.

Nothing here reaches the network: the upstream repository, the fork and the
claim board are bare repositories under a temporary directory, which
AUTOFORM_WORK_GIT_BASE points the loop at, and `gh` and `claude` are small
Python scripts that read and write a JSON state file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the work loop runs on Linux and macOS")

ARTICLE = "af_0123456789abcdef01234567"
OTHER = "af_89abcdef0123456789abcdef"

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys
STATE = os.environ["FAKE_GH_STATE"]
REMOTES = os.environ["FAKE_GH_REMOTES"]
args = sys.argv[1:]
with open(STATE) as f:
    state = json.load(f)
state.setdefault("calls", []).append(args)
def save():
    with open(STATE, "w") as f:
        json.dump(state, f, indent=1)
def out(value):
    save()
    sys.stdout.write(value if isinstance(value, str) else json.dumps(value))
    sys.exit(0)
def stdin_json():
    source = args[args.index("--input") + 1]
    text = sys.stdin.read() if source == "-" else open(source).read()
    return json.loads(text or "null")
def head_of(branch):
    r = subprocess.run(["git", "--git-dir", f"{REMOTES}/octo/proj.git", "rev-parse", f"refs/heads/{branch}"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""
def node(pr):
    head = head_of(pr["branch"])
    checks = [dict(c) for c in pr.get("checks", [])]
    reviews = [dict(r, commit={"oid": head if r.get("commit") == "HEAD" else r.get("commit")}) for r in pr.get("reviews", [])]
    return {"id": f"PR_{pr['number']}", "number": pr["number"], "title": pr["title"], "url": pr["url"],
            "state": pr.get("state", "OPEN"), "isDraft": False, "headRefName": pr["branch"], "headRefOid": head,
            "baseRefName": "main", "mergeable": pr.get("mergeable", "MERGEABLE"), "mergeStateStatus": "CLEAN",
            "isInMergeQueue": False, "headRepository": {"nameWithOwner": "octo/proj"},
            "headRepositoryOwner": {"login": "octo"}, "isCrossRepository": True,
            "createdAt": pr["created_at"], "updatedAt": pr["created_at"], "mergedAt": pr.get("merged_at"),
            "closedAt": None, "body": pr["body"], "author": {"login": "octo"},
            "commits": {"nodes": [{"commit": {"oid": head, "statusCheckRollup": {"contexts": {"nodes": checks}}}}]},
            "reviews": {"nodes": reviews}, "comments": {"nodes": pr.get("comments", [])},
            "reviewThreads": {"nodes": []}}
if args[:2] == ["auth", "token"]:
    out("fake-token\n")
if args[:2] == ["auth", "git-credential"]:
    out("")
if args[0] != "api":
    sys.stderr.write("unsupported: " + " ".join(args)); sys.exit(1)
path = args[1]
method = args[args.index("--method") + 1] if "--method" in args else "GET"
if path == "user":
    out({"login": "octo"})
if path == "graphql":
    fields = dict(a.split("=", 1) for a in args[2:] if "=" in a and not a.startswith("query="))
    query = next(a for a in args if a.startswith("query="))
    if "search(" in query:
        nodes = [node(pr) for pr in state["prs"].values() if pr.get("state", "OPEN") == "OPEN"]
        out({"data": {"search": {"nodes": nodes}}})
    pr = state["prs"].get(fields["n"])
    out({"data": {"repository": {"pullRequest": node(pr) if pr else None}}})
if path == "repos/up/proj":
    out({"full_name": "up/proj", "default_branch": "main", "fork": False})
if path == "repos/octo/proj":
    out({"full_name": "octo/proj", "fork": True, "parent": {"full_name": "up/proj"}, "source": {"full_name": "up/proj"}})
if path.startswith("repos/up/proj/rules/branches/"):
    out([{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "build"}]}}])
if re.match(r"repos/up/proj/commits/[^/]+/check-runs", path):
    out({"check_runs": state.get("base_check_runs", [])})
if path.startswith("repos/up/proj/pulls?state=open"):
    rows = [{"number": pr["number"], "title": pr["title"], "body": pr["body"], "head": {"ref": pr["branch"]},
             "user": {"login": "octo"}, "created_at": pr["created_at"]}
            for pr in state["prs"].values() if pr.get("state", "OPEN") == "OPEN"]
    rows += state.get("other_pulls", [])
    out([rows] if "--slurp" in args else rows)
if path.startswith("repos/up/proj/pulls?state=all"):
    head = re.search(r"head=([^&]+)", path).group(1).split(":", 1)[1]
    out([{"number": pr["number"], "head": {"ref": pr["branch"], "user": {"login": "octo"}}}
         for pr in state["prs"].values() if pr["branch"] == head])
if path == "repos/up/proj/pulls" and method == "POST":
    body = stdin_json()
    number = 100 + len(state["prs"])
    branch = body["head"].split(":", 1)[1]
    state["prs"][str(number)] = {"number": number, "title": body["title"], "body": body["body"], "branch": branch,
                                 "url": f"https://example.invalid/pull/{number}", "created_at": "2026-11-22T10:00:00Z",
                                 "checks": [], "reviews": [], "comments": []}
    out({"number": number, "html_url": f"https://example.invalid/pull/{number}"})
m = re.match(r"repos/up/proj/issues/(\d+)/(comments|labels)", path)
if m and method == "POST":
    body = stdin_json()
    pr = state["prs"][m.group(1)]
    if m.group(2) == "comments":
        pr["comments"].append({"databaseId": 900 + len(pr["comments"]), "author": {"login": "octo", "__typename": "User"},
                               "createdAt": "2026-11-22T11:00:00Z", "body": body["body"], "url": ""})
    else:
        pr.setdefault("labels", []).extend(body["labels"])
    out({"html_url": "https://example.invalid/comment"})
if path == "markdown":
    out("<p>" + stdin_json()["text"] + "</p>")
if "/actions/jobs/" in path:
    out("error: the check failed\n")
if path.startswith("repos/up/proj/forks"):
    out([[]] if "--slurp" in args else [])
sys.stderr.write("HTTP 404: unsupported " + path); sys.exit(1)
'''

FAKE_AGENT = r'''#!/usr/bin/env python3
import json, os, re, sys
prompt = sys.stdin.read()
plan_path = os.environ["FAKE_AGENT_PLAN"]
with open(plan_path) as f:
    plan = json.load(f)
step = plan.pop(0) if plan else {"edit": {"Work.md": "more work\n"}, "report": {"status": "done"}}
with open(plan_path, "w") as f:
    json.dump(plan, f)
with open(os.environ["FAKE_AGENT_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "gh_config": os.environ.get("GH_CONFIG_DIR"),
                        "gh_token": os.environ.get("GH_TOKEN")}) + "\n")
for name, text in step.get("edit", {}).items():
    with open(name, "a") as f:
        f.write(text)
report = re.search(r"Write `([^`]+report\.json)`", prompt).group(1)
with open(report, "w") as f:
    json.dump(step.get("report", {"status": "done"}), f)
print(json.dumps({"type": "result", "total_cost_usd": step.get("cost", 0.25), "num_turns": 3, "usage": {},
                  "session_id": "session-1", "is_error": False, "result": "ok"}))
'''


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    remotes = tmp_path / "remotes"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("project\n")
    (seed / "CONTRIBUTING.md").write_text("Be kind.\n")
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=Seed", "-c", "user.email=seed@example.invalid", "commit", "-q", "-m", "seed")
    for name in ("up/proj", "octo/proj"):
        (remotes / name).parent.mkdir(parents=True, exist_ok=True)
        git(tmp_path, "clone", "-q", "--bare", str(seed), str(remotes / f"{name}.git"))
    git(tmp_path, "init", "-q", "--bare", str(remotes / "octo" / "claims.git"))
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", str(remotes / "up" / "proj.git"), str(checkout))
    git(checkout, "config", "user.name", "Octo Cat")
    git(checkout, "config", "user.email", "octo@example.invalid")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("gh", FAKE_GH), ("claude", FAKE_AGENT)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    (tmp_path / "gh-state.json").write_text(json.dumps({"prs": {}}))
    (tmp_path / "plan.json").write_text("[]")
    tasks = {
        "schema": "autoform-work/v2",
        "items": [
            {"article_id": ARTICLE, "node_id": "ch/result", "claim_target": ARTICLE, "phase": "proof",
             "ready": True, "state": "can_prove", "title": "The result",
             "lean_targets": [{"declaration": "Proj.Ch.great_result", "source_file": None}]},
        ],
    }
    (tmp_path / "tasks.json").write_text(json.dumps(tasks))
    (tmp_path / "profile.toml").write_text(textwrap.dedent('''
        schema = "autoform-work-profile/v1"
        [tasks]
        phases = ["proof"]
        [verify]
        commands = [["python3", "-c", "pass"]]
        statement_fidelity = false
        [pull_request]
        body_lines = ["Part of #1."]
        [review]
        bot = "github-actions"
        markers = ["<!-- codex-automatic-review -->"]
        checks = ["codex-verdict"]
        '''))
    return {"root": tmp_path, "remotes": remotes, "checkout": checkout, "bin": bin_dir}


def run_loop(world: dict[str, Path], *extra: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    root = world["root"]
    env = dict(os.environ)
    env.update(
        PATH=f"{world['bin']}{os.pathsep}{env.get('PATH', '')}",
        FAKE_GH_STATE=str(root / "gh-state.json"),
        FAKE_GH_REMOTES=str(world["remotes"]),
        FAKE_AGENT_PLAN=str(root / "plan.json"),
        FAKE_AGENT_LOG=str(root / "agent-log.jsonl"),
        AUTOFORM_WORK_GIT_BASE="file://" + str(world["remotes"]),
        GH_TOKEN="secret-token",
        XDG_STATE_HOME=str(root / "xdg-state"),
        XDG_CACHE_HOME=str(root / "xdg-cache"),
    )
    command = [
        sys.executable, "-m", "autoform_cli", "work", "run", "--repo", "up/proj", "--checkout", str(world["checkout"]),
        "--profile", str(root / "profile.toml"), "--tasks", str(root / "tasks.json"), "--agent", "claude",
        "--claims-repo", "file://" + str(world["remotes"] / "octo" / "claims.git"),
        "--state-dir", str(root / "state"), *extra,
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300)
    if check and result.returncode != 0:
        raise AssertionError(f"exit {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def gh_state(world: dict[str, Path]) -> dict:
    return json.loads((world["root"] / "gh-state.json").read_text())


def set_gh_state(world: dict[str, Path], state: dict) -> None:
    (world["root"] / "gh-state.json").write_text(json.dumps(state))


def claim_refs(world: dict[str, Path]) -> list[str]:
    out = git(world["remotes"] / "octo" / "claims.git", "for-each-ref", "--format=%(refname)", "refs/autoform-claims/")
    return out.split()


def events(world: dict[str, Path]) -> list[dict]:
    path = world["root"] / "state" / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def check_run(name: str, conclusion: str = "SUCCESS", status: str = "COMPLETED") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "startedAt": "2026-11-22T10:05:00Z", "detailsUrl": "https://example.invalid/job/7",
            "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}}}


def review(state: str, commit: str = "HEAD") -> dict:
    return {"databaseId": 55, "author": {"login": "github-actions", "__typename": "Bot"}, "state": state,
            "submittedAt": "2026-11-22T10:10:00Z", "commit": commit,
            "body": "<!-- codex-automatic-review -->\nThis is a formal review.\n- P1: the proof misses a case."}


def test_a_round_opens_a_verified_pull_request_and_claims_its_task(world):
    (world["root"] / "plan.json").write_text(json.dumps([
        {"edit": {"Proof.md": "the proof\n"},
         "report": {"status": "done", "pr_title": "Prove the great result",
                    "pr_body": "## Summary\n\nProves the result.", "commit_message": "Prove the great result"},
         "cost": 1.5},
    ]))
    result = run_loop(world)
    state = gh_state(world)
    assert list(state["prs"]) == ["100"], result.stdout
    pr = state["prs"]["100"]
    assert pr["title"] == "Prove the great result"
    assert pr["branch"].startswith("autoform-work/great-result-")
    body = pr["body"]
    assert f"Tasks: {ARTICLE}" in body and "Part of #1." in body and "Autoform project: up/proj/blueprint" in body
    assert "<!-- autoform-work " in body
    # The branch landed on the fork only, by explicit refspec; upstream main did not move.
    fork_head = git(world["remotes"] / "octo" / "proj.git", "rev-parse", f"refs/heads/{pr['branch']}")
    message = git(world["remotes"] / "octo" / "proj.git", "log", "-1", "--format=%B", fork_head)
    assert message.startswith("Prove the great result") and f"Tasks: {ARTICLE}" in message
    assert "Co-authored-by: Claude <noreply@anthropic.com>" in message
    assert git(world["remotes"] / "octo" / "proj.git", "log", "-1", "--format=%an", fork_head) == "Octo Cat"
    upstream = world["remotes"] / "up" / "proj.git"
    assert git(upstream, "rev-parse", "refs/heads/main") == git(world["checkout"], "rev-parse", "origin/main")
    # The task's claim is held, and its note names the pull request.
    refs = claim_refs(world)
    assert len(refs) == 1
    note = git(world["remotes"] / "octo" / "claims.git", "log", "-1", "--format=%B", refs[0])
    assert '\\"pr\\": 100' in note or '"pr": 100' in note
    # The agent never saw GitHub credentials.
    log = [json.loads(line) for line in (world["root"] / "agent-log.jsonl").read_text().splitlines()]
    assert log[0]["gh_token"] is None and log[0]["gh_config"].endswith("no-gh")
    assert "skills/formalize/references/leaf.md" in log[0]["prompt"]
    spent = [row for row in events(world) if row["event"] == "agent"]
    assert spent[0]["cost_usd"] == 1.5


def test_merge_ready_releases_the_claim_and_changes_nothing(world):
    run_loop(world)
    state = gh_state(world)
    state["prs"]["100"]["checks"] = [check_run("build"), check_run("codex-verdict")]
    state["prs"]["100"]["reviews"] = [review("APPROVED")]
    set_gh_state(world, state)
    pushes_before = len([row for row in events(world) if row["event"] == "pushed"])
    run_loop(world, "--max-attempts", "1")
    names = [row["event"] for row in events(world)]
    assert "merge-ready" in names
    assert claim_refs(world) == []
    assert len([row for row in events(world) if row["event"] == "pushed"]) == pushes_before


def test_a_change_request_at_the_head_is_fixed_and_pushed_on_the_same_branch(world):
    run_loop(world)
    state = gh_state(world)
    branch = state["prs"]["100"]["branch"]
    first = git(world["remotes"] / "octo" / "proj.git", "rev-parse", f"refs/heads/{branch}")
    state["prs"]["100"]["checks"] = [check_run("build", "CANCELLED"), check_run("codex-verdict", "FAILURE")]
    state["prs"]["100"]["reviews"] = [review("CHANGES_REQUESTED")]
    set_gh_state(world, state)
    (world["root"] / "plan.json").write_text(json.dumps([
        {"edit": {"Proof.md": "the missing case\n"},
         "report": {"status": "done", "commit_message": "Cover the missing case"}},
    ]))
    run_loop(world, "--max-attempts", "1")
    second = git(world["remotes"] / "octo" / "proj.git", "rev-parse", f"refs/heads/{branch}")
    assert second != first
    assert git(world["remotes"] / "octo" / "proj.git", "merge-base", "--is-ancestor", first, second) == ""
    prompt = [json.loads(line) for line in (world["root"] / "agent-log.jsonl").read_text().splitlines()][-1]["prompt"]
    assert "the proof misses a case" in prompt
    record = json.loads((world["root"] / "state" / "prs" / "100.json").read_text())
    assert record["repairs"] == 1 and "review:55" in record["handled"]


def test_contested_findings_get_a_reply_and_no_push(world):
    run_loop(world)
    state = gh_state(world)
    state["prs"]["100"]["reviews"] = [review("CHANGES_REQUESTED")]
    set_gh_state(world, state)
    (world["root"] / "plan.json").write_text(json.dumps([
        {"report": {"status": "done", "contested": [{"finding": "P1", "reason": "the case is covered by lemma x"}]}},
    ]))
    run_loop(world, "--max-attempts", "1")
    comments = gh_state(world)["prs"]["100"]["comments"]
    assert any("the case is covered by lemma x" in comment["body"] for comment in comments)
    assert all("<!-- autoform-work" in comment["body"] for comment in comments)


def test_a_person_s_question_is_answered_once(world):
    run_loop(world)
    state = gh_state(world)
    state["prs"]["100"]["comments"] = [{"databaseId": 7, "author": {"login": "alice", "__typename": "User"},
                                        "createdAt": "2026-11-22T10:30:00Z", "body": "Why this lemma?", "url": ""}]
    set_gh_state(world, state)
    (world["root"] / "plan.json").write_text(json.dumps([{"report": {"status": "reply", "reply": "It gives the bound."}}]))
    run_loop(world, "--max-attempts", "1")
    run_loop(world, "--max-attempts", "1")
    comments = gh_state(world)["prs"]["100"]["comments"]
    replies = [comment for comment in comments if "It gives the bound." in comment["body"]]
    assert len(replies) == 1


def test_a_blocked_attempt_releases_its_claim_and_opens_nothing(world):
    (world["root"] / "plan.json").write_text(json.dumps([
        {"report": {"status": "blocked", "notes": "the statement is false for n = 0"}},
    ]))
    run_loop(world)
    assert gh_state(world)["prs"] == {}
    assert claim_refs(world) == []
    failures = (world["root"] / "state" / "failures.jsonl").read_text()
    assert "the statement is false for n = 0" in failures


def test_a_dry_run_pushes_nothing(world):
    run_loop(world, "--dry-run")
    assert gh_state(world)["prs"] == {}
    assert claim_refs(world) == []
    fork = world["remotes"] / "octo" / "proj.git"
    assert git(fork, "for-each-ref", "--format=%(refname)", "refs/heads/autoform-work/") == ""
    assert any(row["event"] == "dry-run-ready" for row in events(world))


def test_a_task_another_pull_request_names_is_never_picked(world):
    state = gh_state(world)
    state["other_pulls"] = [{"number": 5, "title": "Prove it", "body": f"Tasks: {ARTICLE}", "head": {"ref": "x"},
                             "user": {"login": "bob"}, "created_at": "2026-11-21T00:00:00Z"}]
    set_gh_state(world, state)
    result = run_loop(world)
    assert gh_state(world)["prs"] == {}
    assert "no-free-task" in result.stdout


def test_a_stop_file_stops_the_loop_before_any_round(world):
    (world["root"] / "state").mkdir()
    (world["root"] / "state" / "STOP").write_text("")
    result = run_loop(world, "--loop")
    assert "stopped" in result.stdout and gh_state(world)["prs"] == {}
