"""Unit tests of the work loop's decisions: profiles, agents, pull-request state, tasks, verification, Git guards."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from autoform_cli.__main__ import _expand_work_shorthand
from autoform_cli.work_loop import gitops, prstate, tasks, verify
from autoform_cli.work_loop.agents import (
    AgentRunner,
    AgentSpec,
    Stopped,
    agent_command,
    agent_env,
    parse_agent_spec,
    parse_price,
    parse_usage,
)
from autoform_cli.work_loop.cli import _child_argv
from autoform_cli.work_loop.profile import PROFILE_SCHEMA, Profile, ProfileError, load_profile, parse_profile
from autoform_cli.work_loop.state import State
from autoform_cli.work_loop.worker import parse_marker, pull_request_text, render, slug

posix_only = pytest.mark.skipif(os.name != "posix", reason="the work loop runs on Linux and macOS")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------- command line


def test_work_shorthand_runs_the_loop_and_leaves_other_work_commands_alone():
    assert _expand_work_shorthand(["work", "--loop", "--workers", "2"]) == ["work", "run", "--loop", "--workers", "2"]
    assert _expand_work_shorthand(["work", "list", "."]) == ["work", "list", "."]
    assert _expand_work_shorthand(["work", "--help"]) == ["work", "--help"]
    assert _expand_work_shorthand(["work"]) == ["work"]
    assert _expand_work_shorthand(["claim", "--loop"]) == ["claim", "--loop"]


def test_child_workers_get_an_index_and_the_run_but_not_the_worker_count():
    argv = ["work", "run", "--loop", "--workers", "3", "--agent", "codex", "--workers=4"]
    assert _child_argv(argv, 2, "r1") == ["work", "run", "--loop", "--agent", "codex", "--worker-index", "2",
                                          "--run-id", "r1"]


# ---------------------------------------------------------------- profiles


def profile_text(**tables: str) -> str:
    return f'schema = "{PROFILE_SCHEMA}"\n' + "\n".join(f"[{name}]\n{body}" for name, body in tables.items())


def test_the_built_in_atlas_profile_loads_with_its_instructions(tmp_path):
    profile = load_profile(tmp_path, "facebookresearch/atlas-lean")
    assert profile.task_command and "scripts/next_task.py" in profile.task_command
    assert profile.phases == ("proof",)
    assert profile.review_bot == "github-actions" and "<!-- codex-automatic-review -->" in profile.review_markers
    assert "WantedExt" in profile.instructions
    assert profile.publish_check and profile.publish_check_public == "--public"


def test_a_checkout_profile_wins_over_the_built_in_one_and_an_explicit_one_wins_over_both(tmp_path):
    (tmp_path / ".autoform").mkdir()
    (tmp_path / ".autoform" / "work.toml").write_text(profile_text(tasks='phases = ["statement"]'))
    assert load_profile(tmp_path, "facebookresearch/atlas-lean").phases == ("statement",)
    explicit = tmp_path / "mine.toml"
    explicit.write_text(profile_text(pull_request='branch_prefix = "mine/"'))
    assert load_profile(tmp_path, "facebookresearch/atlas-lean", explicit).branch_prefix == "mine/"
    assert load_profile(tmp_path / ".autoform", "someone/else") == Profile()


@pytest.mark.parametrize(
    "text",
    [
        "schema = 'other/v1'\n",
        profile_text(tasks="phase = ['proof']"),
        profile_text(tasks="phases = ['lemma']"),
        profile_text(pull_request="branch_prefix = 'no-slash'"),
        profile_text(pull_request="branch_prefix = '../up/'"),
        profile_text(verify="commands = [[]]"),
        profile_text(verify="statement_fidelity = 'yes'"),
        profile_text(agent="instructions = '../outside.md'"),
        profile_text(mystery="x = 1"),
    ],
)
def test_a_profile_with_a_wrong_key_or_value_is_refused(tmp_path, text):
    with pytest.raises(ProfileError):
        parse_profile(text, source="test", base_dir=tmp_path)


# ---------------------------------------------------------------- agents


def test_agent_specs_and_prices_parse_or_refuse():
    assert parse_agent_spec("claude") == AgentSpec("claude")
    assert parse_agent_spec("codex:gpt-x:high") == AgentSpec("codex", "gpt-x", "high")
    assert parse_agent_spec("claude::max") == AgentSpec("claude", None, "max")
    for bad in ("gemini", "claude:a:b:c", ""):
        with pytest.raises(ValueError):
            parse_agent_spec(bad)
    assert parse_price("input=2,cached=0.5,output=8").output == 8
    assert parse_price("input=2,cached=0.5,output=8,write=3").cache_write == 3
    for bad in ("input=2", "input=2,cached=1,output=x", "input=-1,cached=0,output=1", "input=1,cached=1,output=1,x=1"):
        with pytest.raises(ValueError):
            parse_price(bad)


def test_agent_commands_read_the_prompt_from_standard_input_and_resume_a_session(tmp_path):
    claude = agent_command(AgentSpec("claude", "m", "max"), cwd=tmp_path, resume="s1", budget_usd=5,
                           extra_args=["--bare"])
    assert claude[:2] == ["claude", "-p"] and "--dangerously-skip-permissions" in claude
    assert claude[claude.index("--resume") + 1] == "s1" and claude[claude.index("--max-budget-usd") + 1] == "5"
    assert claude[-1] == "--bare"
    codex = agent_command(AgentSpec("codex", "gpt", "high"), cwd=tmp_path, resume="t1")
    assert codex[:3] == ["codex", "exec", "--json"] and codex[-3:] == ["resume", "t1", "-"]
    assert 'model_reasoning_effort="high"' in codex
    assert agent_command(AgentSpec("codex"), cwd=tmp_path)[-1] == "-"


def test_the_agent_environment_carries_no_github_credentials(tmp_path):
    env = agent_env({"GH_TOKEN": "a", "GITHUB_TOKEN": "b", "PATH": "/bin"}, scratch=tmp_path)
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert Path(env["GH_CONFIG_DIR"]).is_dir() and not any(Path(env["GH_CONFIG_DIR"]).iterdir())
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["PATH"] == "/bin"


def test_claude_usage_takes_the_largest_cumulative_cost_and_sees_a_rate_limit(tmp_path):
    transcript = tmp_path / "t.jsonl"
    rows = [
        {"type": "result", "total_cost_usd": 2.0, "num_turns": 5, "session_id": "s", "usage": {"output_tokens": 10},
         "is_error": True, "api_error_status": 429, "result": "rate limited"},
        {"type": "result", "total_cost_usd": 3.5, "num_turns": 4, "session_id": "s", "usage": {"output_tokens": 7},
         "is_error": False, "result": "done"},
    ]
    transcript.write_text("not json\n" + "\n".join(json.dumps(row) for row in rows) + "\n")
    usage = parse_usage("claude", transcript)
    assert usage.cost_usd == 3.5 and usage.turns == 9 and usage.output_tokens == 17 and usage.segments == 2
    assert not usage.rate_limited
    transcript.write_text(json.dumps(rows[0]) + "\n")
    assert parse_usage("claude", transcript).rate_limited


def test_codex_usage_is_priced_only_with_a_price(tmp_path):
    transcript = tmp_path / "t.jsonl"
    events = [
        {"type": "thread.started", "thread_id": "th"},
        {"type": "turn.completed", "usage": {"input_tokens": 1_000_000, "cached_input_tokens": 400_000,
                                             "cache_write_input_tokens": 100_000, "output_tokens": 100_000}},
        {"type": "error", "message": "Retrying after 429"},
    ]
    transcript.write_text("\n".join(json.dumps(event) for event in events))
    assert parse_usage("codex", transcript).cost_usd is None
    usage = parse_usage("codex", transcript, parse_price("input=2,cached=0.5,output=10,write=4"))
    assert usage.cost_usd == pytest.approx(0.5 * 2 + 0.4 * 0.5 + 0.1 * 4 + 0.1 * 10)
    assert parse_usage("codex", transcript, parse_price("input=2,cached=0.5,output=10")).cost_usd == pytest.approx(
        0.6 * 2 + 0.4 * 0.5 + 0.1 * 10)
    assert usage.session == "th" and not usage.rate_limited
    transcript.write_text("\n".join(json.dumps(event) for event in [*events, {"type": "turn.failed",
                                                                               "error": {"message": "429 Too Many"}}]))
    assert parse_usage("codex", transcript).rate_limited


@posix_only
def test_the_runner_resumes_a_rate_limited_session_and_stops_on_request(tmp_path):
    script = tmp_path / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        sys.stdin.read()
        resumed = "--resume" in sys.argv
        print(json.dumps({{"type": "result", "total_cost_usd": 2.0 if resumed else 1.0, "num_turns": 1,
                          "session_id": "s9", "usage": {{}}, "is_error": not resumed,
                          "api_error_status": None if resumed else 429, "result": "x"}}))
        """))
    script.chmod(0o755)
    env = {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    pauses: list[float] = []
    runner = AgentRunner(env=env, scratch=tmp_path / "s", sleep=pauses.append)
    usage = runner.run(AgentSpec("claude"), "go", cwd=tmp_path, transcript=tmp_path / "t" / "a.jsonl", timeout=60)
    assert usage.resumes == 1 and usage.cost_usd == 2.0 and not usage.rate_limited and pauses
    runner.stop()
    with pytest.raises(Stopped):
        runner.run(AgentSpec("claude"), "go", cwd=tmp_path, transcript=tmp_path / "t" / "b.jsonl", timeout=60)


@posix_only
def test_the_runner_kills_an_agent_that_runs_out_of_time(tmp_path):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    script.chmod(0o755)
    runner = AgentRunner(env={"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}, scratch=tmp_path / "s")
    usage = runner.run(AgentSpec("claude"), "go", cwd=tmp_path, transcript=tmp_path / "t.jsonl", timeout=1)
    assert usage.timed_out and usage.exit_status is None


# ---------------------------------------------------------------- pull-request state


def check(name: str, conclusion: str = "SUCCESS", status: str = "COMPLETED", started: str = "2026-01-01T00:00:00Z",
          workflow: str = "CI") -> dict:
    return {"name": name, "workflow": workflow, "status": status, "conclusion": conclusion, "started_at": started,
            "url": ""}


def pr(**fields) -> dict:
    base = {"state": "OPEN", "head": "h1", "mergeable": "MERGEABLE", "draft": False, "checks": [check("build")],
            "reviews": [], "comments": []}
    return {**base, **fields}


def bot_review(state: str, commit: str = "h1", at: str = "2026-01-01T01:00:00Z", marker: str = "<!-- codex-automatic-review -->",
               rid: str = "review:1") -> dict:
    return {"id": rid, "author": "github-actions", "state": state, "submitted_at": at, "body": marker, "commit": commit}


def decide(value: dict, record: dict | None = None, **options) -> prstate.Action:
    defaults = {"required": {"build"}, "review_bot": "github-actions",
                "review_markers": ["<!-- codex-automatic-review -->", "<!-- codex-comment-review -->"],
                "review_checks": ["codex-verdict"]}
    return prstate.next_action(value, record or {}, **{**defaults, **options})


def test_the_latest_run_of_a_check_counts_and_review_checks_are_left_to_the_verdict():
    value = pr(checks=[check("build", "FAILURE", started="2026-01-01T00:00:00Z"),
                       check("build", "SUCCESS", started="2026-01-01T02:00:00Z"),
                       check("codex-verdict", "FAILURE"), check("lint", "", status="IN_PROGRESS"),
                       check("docs", "CANCELLED"), check("first-time", "ACTION_REQUIRED")])
    summary = prstate.summarize_checks(value, required={"build", "guard"}, ignore={"codex-verdict"})
    assert summary.passed == ("CI / build",) and summary.pending == ("CI / lint",)
    assert summary.cancelled == ("CI / docs",) and summary.waiting == ("CI / first-time",)
    assert summary.failed == () and summary.missing_required == ("guard",)


def test_the_latest_formal_verdict_counts_and_informal_comments_do_not():
    value = pr(reviews=[bot_review("APPROVED", at="2026-01-01T01:00:00Z"),
                        bot_review("CHANGES_REQUESTED", at="2026-01-01T03:00:00Z", marker="<!-- codex-comment-review -->",
                                   rid="review:2"),
                        bot_review("APPROVED", at="2026-01-01T04:00:00Z", marker="<!-- codex-response -->",
                                   rid="review:3")])
    verdict = prstate.review_verdict(value, bot="github-actions[bot]",
                                     markers=["<!-- codex-automatic-review -->", "<!-- codex-comment-review -->"])
    assert verdict["id"] == "review:2"
    assert prstate.review_verdict(value, bot=None) is None


def test_people_comments_exclude_bots_the_loop_handled_and_resolved_threads():
    comments = [
        {"id": "issue:1", "kind": "comment", "author": "alice", "bot": False, "body": "Please rename."},
        {"id": "issue:2", "kind": "comment", "author": "github-actions", "bot": True, "body": "codex"},
        {"id": "issue:3", "kind": "comment", "author": "me", "bot": False, "body": "done <!-- autoform-work {} -->"},
        {"id": "line:4", "kind": "line", "author": "bob", "bot": False, "body": "nit", "resolved": True},
        {"id": "issue:5", "kind": "comment", "author": "carol", "bot": False, "body": "old"},
    ]
    people = prstate.human_comments(pr(comments=comments), handled={"issue:5"})
    assert [comment["id"] for comment in people] == ["issue:1"]


def test_a_round_takes_a_person_first_then_a_conflict_then_a_change_request_then_a_failed_check():
    person = {"id": "issue:1", "kind": "comment", "author": "alice", "bot": False, "body": "?"}
    failing = [check("build", "FAILURE")]
    asks = [bot_review("CHANGES_REQUESTED")]
    assert decide(pr(comments=[person], mergeable="CONFLICTING", reviews=asks, checks=failing)).kind == "human"
    assert decide(pr(mergeable="CONFLICTING", reviews=asks, checks=failing)).kind == "merge-base"
    assert decide(pr(reviews=asks, checks=failing)).kind == "fix-review"
    assert decide(pr(checks=failing)).kind == "fix-ci"
    assert decide(pr(reviews=[bot_review("APPROVED")], checks=[check("build"), check("codex-verdict")])).kind == "ready"


def test_a_handled_or_stale_change_request_is_not_fixed_again():
    asks = [bot_review("CHANGES_REQUESTED")]
    assert decide(pr(reviews=asks), {"handled": ["review:1"]}).kind == "wait"
    assert decide(pr(reviews=[bot_review("CHANGES_REQUESTED", commit="old")])).kind == "wait"


def test_a_check_that_fails_on_the_base_too_is_waited_out_not_repaired():
    action = decide(pr(checks=[check("build", "FAILURE")]), base_failures={"build"})
    assert action.kind == "wait"
    assert decide(pr(checks=[check("build", "FAILURE")]), {"handled": ["ci:h1"]}).kind == "wait"


def test_a_spent_repair_budget_parks_the_pull_request_but_a_person_is_still_answered():
    failing = pr(checks=[check("build", "FAILURE")])
    assert decide(failing, {"repairs": 3}).kind == "park"
    person = {"id": "issue:9", "kind": "comment", "author": "alice", "bot": False, "body": "?"}
    assert decide(pr(comments=[person], checks=[check("build", "FAILURE")]), {"repairs": 3}).kind == "human"
    assert decide(pr(mergeable="CONFLICTING"), {"merges": 5}).kind == "park"
    assert decide(failing, {"parked": "x"}).kind == "wait"
    assert decide(pr(state="MERGED")).kind == "closed"


@pytest.mark.parametrize(
    ("change", "waits_for"),
    [
        ({"draft": True}, "draft"),
        ({"checks": [check("build", "", status="QUEUED")]}, "waiting for checks"),
        ({"checks": []}, "waiting for checks"),
        ({"checks": [check("build"), check("x", "ACTION_REQUIRED")]}, "maintainer"),
        ({"checks": [check("build", "CANCELLED")]}, "re-run"),
        ({"reviews": []}, "review of the head"),
        ({"reviews": [bot_review("APPROVED", commit="older")]}, "review of the head"),
        ({"mergeable": "UNKNOWN"}, "mergeable"),
    ],
)
def test_merge_ready_needs_every_check_green_the_bot_s_approval_of_the_head_and_a_clean_merge(change, waits_for):
    value = pr(**{"reviews": [bot_review("APPROVED")], **change})
    action = decide(value)
    assert action.kind == "wait" and waits_for in action.reason


def test_without_a_review_bot_green_checks_are_enough():
    assert decide(pr(), review_bot=None).kind == "ready"


# ---------------------------------------------------------------- tasks


TASK = {"article_id": "af_0123456789abcdef01234567", "node_id": "a/b", "phase": "proof", "ready": True,
        "lean_targets": [{"declaration": "A.b"}]}


def test_a_pull_request_takes_a_task_only_by_naming_its_whole_article_id():
    other = dict(TASK, article_id="af_ffffffffffffffffffffffff", node_id="c/d")
    pulls = [{"title": "Prove b", "body": "Tasks: AF_0123456789ABCDEF01234567", "branch": ""},
             {"title": "x", "body": "af_ffffffffffffffffffffffff0", "branch": ""}]
    assert tasks.generic_taken([TASK, other], pulls) == {TASK["article_id"]}


def test_candidates_are_random_free_tasks_of_the_allowed_phases():
    pool = [dict(TASK, article_id=f"af_{index:024x}", node_id=f"n/{index}") for index in range(30)]
    pool.append(dict(TASK, article_id="af_" + "e" * 24, node_id="s/1", phase="statement"))
    first = tasks.candidates(pool, phases=["proof"], skip={pool[0]["article_id"]}, rng=random.Random(1), limit=10)
    second = tasks.candidates(pool, phases=["proof"], skip={pool[0]["article_id"]}, rng=random.Random(2), limit=10)
    assert len(first) == 10 and first != second
    assert all(item["phase"] == "proof" and item["article_id"] != pool[0]["article_id"] for item in first)
    only = tasks.candidates(pool, phases=["proof", "statement"], only="s/1")
    assert [item["node_id"] for item in only] == ["s/1"]


def test_a_picker_command_supplies_the_free_tasks_and_the_full_list_for_the_self_check(tmp_path):
    items = [TASK, dict(TASK, article_id="af_" + "1" * 24, node_id="c/d")]
    task_file = tmp_path / "tasks.json"
    task_file.write_text(json.dumps({"schema": "autoform-work/v2", "items": items}))
    picker = tmp_path / "pick.py"
    picker.write_text(textwrap.dedent("""\
        import json, sys
        source = sys.argv[sys.argv.index("--tasks") + 1]
        items = json.load(open(source))["items"]
        print(json.dumps({"tasks": source, "commit": "abc", "taken": 1, "next": items[1:]}))
        """))
    profile = Profile(task_command=(sys.executable, str(picker)), tasks_option="--tasks")
    found = tasks.load_tasks(profile, tmp_path, tasks_override=str(task_file), env=dict(os.environ),
                             open_pulls=lambda: pytest.fail("the picker reads pull requests itself"))
    assert [tasks.task_id(item) for item in found.free] == ["af_" + "1" * 24]
    assert len(found.items) == 2 and found.commit == "abc" and found.taken == 1


def test_the_generic_source_leaves_out_tasks_open_pull_requests_name(tmp_path):
    task_file = tmp_path / "tasks.json"
    task_file.write_text(json.dumps([TASK, dict(TASK, article_id="af_" + "1" * 24, node_id="c/d")]))
    found = tasks.load_tasks(Profile(), tmp_path, tasks_override=str(task_file), env=dict(os.environ),
                             open_pulls=lambda: [{"title": "", "body": f"Tasks: {TASK['article_id']}", "branch": ""}])
    assert [tasks.task_id(item) for item in found.free] == ["af_" + "1" * 24]


def test_the_self_check_uses_the_project_s_taken_function(tmp_path):
    (tmp_path / "taken.py").write_text(textwrap.dedent("""\
        def taken(items, pulls):
            text = pulls[0]["title"] + pulls[0]["body"] + pulls[0]["body_html"] + pulls[0]["head"]["ref"]
            return {item["node_id"]: [0] for item in items if item["lean_targets"][0]["declaration"] in text}
        """))
    other = dict(TASK, article_id="af_" + "1" * 24, node_id="c/d", lean_targets=[{"declaration": "C.d"}])
    profile = Profile(taken_module="taken.py")
    named = tasks.tasks_named_by(profile, tmp_path, [TASK, other], title="Prove A.b", body="uses C.d", branch="x",
                                 body_html=lambda: "<p>uses C.d</p>")
    assert named == {TASK["article_id"], other["article_id"]}
    with pytest.raises(tasks.TaskError):
        tasks.load_taken_function(tmp_path, "../outside.py")


# ---------------------------------------------------------------- verification


def test_forbidden_tokens_count_in_code_but_not_in_comments_docstrings_or_strings():
    text = textwrap.dedent("""\
        /-- We never use sorry here. -/
        theorem t : True := by
          -- admit nothing
          exact trivial
        def s : String := "sorry"
        theorem u : False := by sorry
        axiom bad : False
        private axiom worse : False
        theorem v : 1 = 1 := by native_decide
        /- nested /- sorry -/ still a comment -/
        """)
    hits = verify.forbidden_hits(text, range(1, 11), verify.FORBIDDEN)
    assert [number for number, _ in hits] == [6, 7, 8, 9]
    assert verify.forbidden_hits(text, [7], verify.FORBIDDEN) == [(7, "axiom bad : False")]


def facts(statement: str = "forall x, P x", *, kind: str = "theorem", wanted: bool = False, axioms=("propext",),
          closure=None) -> dict:
    return {"found": True, "kind": kind, "wanted": wanted, "statement": statement, "levelParams": [],
            "axioms": list(axioms), "closure": closure if closure is not None else
            [{"name": "_private.WantedExt.M.0.Pdef", "kind": "def", "type": "Nat -> Prop", "valueHash": "1",
              "ctorTypes": [], "levelParams": []}]}


def test_fidelity_accepts_the_same_statement_proved_elsewhere_and_refuses_any_drift():
    base = {"A.b": facts(kind="def", wanted=True, axioms=())}
    moved = facts(closure=[{"name": "_private.MathlibExt.N.0.Pdef", "kind": "def", "type": "Nat -> Prop",
                            "valueHash": "1", "ctorTypes": [], "levelParams": []}])
    assert verify.fidelity_problems(base, {"A.b": moved}, phase="proof") == []
    assert verify.fidelity_problems(base, {"A.b": facts("forall x, Q x")}, phase="proof")
    assert verify.fidelity_problems(base, {"A.b": facts(kind="def", wanted=True)}, phase="proof")
    assert verify.fidelity_problems(base, {"A.b": facts(axioms=("sorryAx",))}, phase="proof")
    changed = facts(closure=[{"name": "_private.WantedExt.M.0.Pdef", "kind": "def", "type": "Nat -> Prop",
                              "valueHash": "2", "ctorTypes": [], "levelParams": []}])
    assert any("valueHash" in problem for problem in verify.fidelity_problems(base, {"A.b": changed}, phase="proof"))
    assert verify.fidelity_problems(base, {"A.b": {"found": False}}, phase="proof")


@posix_only
def test_verification_diffs_against_the_base_and_runs_the_profile_commands(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    empty = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert not empty.ok and empty.steps[-1]["step"] == "the change is not empty"
    (tmp_path / "b.lean").write_text("-- a comment with sorry\ntheorem x : True := by sorry\n")
    profile = Profile(verify_commands=((sys.executable, "-c", "raise SystemExit(3)"),))
    result = verify.run_steps(tmp_path, base=base, profile=profile, env=dict(os.environ), targets=None, phase=None)
    failed = [step["step"] for step in result.failures()]
    assert failed == ["no proof holes or escapes in added Lean lines"], result.steps
    (tmp_path / "b.lean").write_text("theorem x : True := trivial\n")
    result = verify.run_steps(tmp_path, base=base, profile=profile, env=dict(os.environ), targets=None, phase=None)
    assert [step["step"] for step in result.failures()] == [" ".join(profile.verify_commands[0])]


# ---------------------------------------------------------------- Git guards


@posix_only
def test_pushes_stay_on_prefixed_branches_and_respect_the_lease(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "f").write_text("1\n")
    git(work, "add", "-A")
    git(work, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "one")
    first = git(work, "rev-parse", "HEAD")
    options = {"url": str(remote), "prefix": "loop/", "protected": ["main"], "gh": "gh"}
    for branch in ("main", "loop/", "other/x", "loop/a..b"):
        with pytest.raises(gitops.GitError):
            gitops.safe_push(work, commit_sha=first, branch=branch, old=None, **options)
    gitops.safe_push(work, commit_sha=first, branch="loop/x", old=None, **options)
    (work / "f").write_text("2\n")
    git(work, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-am", "two")
    second = git(work, "rev-parse", "HEAD")
    # A create-only push refuses a branch that exists, and a lease refuses a branch that moved.
    with pytest.raises(gitops.GitError):
        gitops.safe_push(work, commit_sha=second, branch="loop/x", old=None, **options)
    with pytest.raises(gitops.GitError):
        gitops.safe_push(work, commit_sha=second, branch="loop/x", old="0" * 40, **options)
    gitops.safe_push(work, commit_sha=second, branch="loop/x", old=first, **options)
    assert git(remote, "rev-parse", "refs/heads/loop/x") == second
    assert git(remote, "for-each-ref", "--format=%(refname)", "refs/heads/main") == ""


@posix_only
def test_a_worktree_shares_the_checkout_s_packages_and_keeps_its_build_across_resets(tmp_path):
    checkout = tmp_path / "checkout"
    git(tmp_path, "init", "-q", "-b", "main", str(checkout))
    (checkout / "lake-manifest.json").write_text('{"packages": []}\n')
    (checkout / ".gitignore").write_text(".lake\n")
    git(checkout, "add", "-A")
    git(checkout, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "one")
    (checkout / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    (checkout / ".lake" / "build").mkdir()
    (checkout / ".lake" / "build" / "A.olean").write_text("olean")
    worktree = gitops.ensure_worktree(checkout, tmp_path / "wt", "HEAD")
    done = gitops.prepare_lake(worktree, checkout)
    assert (worktree / ".lake" / "packages").is_symlink() and len(done) == 2
    (worktree / ".lake" / "build" / "B.olean").write_text("mine")
    (worktree / "junk.txt").write_text("x")
    gitops.reset_worktree(worktree, git(checkout, "rev-parse", "HEAD"))
    assert not (worktree / "junk.txt").exists() and (worktree / ".lake" / "build" / "B.olean").exists()
    assert not (checkout / ".lake" / "build" / "B.olean").exists()
    (worktree / "lake-manifest.json").write_text('{"packages": ["new"]}\n')
    gitops.prepare_lake(worktree, checkout)
    assert not (worktree / ".lake" / "packages").exists()
    with pytest.raises(gitops.GitError):
        gitops.ensure_worktree(checkout, tmp_path / "checkout" / "lake-manifest.json", "HEAD")


def test_github_remote_urls_name_their_repository():
    assert gitops.github_repository("https://github.com/o/r.git") == "o/r"
    assert gitops.github_repository("git@github.com:o/r") == "o/r"
    assert gitops.github_repository("file:///tmp/r") is None
    env = gitops.credential_env({"GIT_CONFIG_COUNT": "1"}, "/bin/gh")
    assert env["GIT_CONFIG_COUNT"] == "3" and env["GIT_CONFIG_VALUE_2"] == "!/bin/gh auth git-credential"


# ---------------------------------------------------------------- state and pull-request text


@posix_only
def test_records_add_and_count_and_budgets_sum_one_run(tmp_path):
    state = State(tmp_path, worker="w1", run="r1")
    state.update_pr(7, add_handled=["a"], inc_repairs=1)
    record = state.update_pr(7, add_handled=["b", "a"], inc_repairs=1, parked="x")
    assert record["handled"] == ["a", "b"] and record["repairs"] == 2 and record["parked"] == "x"
    state.event("agent", cost_usd=1.25)
    state.event("agent", cost_usd=None)
    State(tmp_path, worker="w2", run="r2").event("agent", cost_usd=10)
    state.event("author-start", task="t")
    assert state.spent(run="r1") == 1.25 and state.spent() == 11.25 and state.attempts(run="r1") == 1
    state.record_failure("t", "blocked")
    assert state.failed_recently() == {"t"} and state.failed_recently(days=0) == set()
    assert state.stop_file() is None
    (tmp_path / "workers" / "w1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workers" / "w1" / "STOP").write_text("")
    assert state.stop_file() is not None and State(tmp_path, worker="w2").stop_file() is None


def test_pull_request_text_carries_the_task_line_the_project_line_and_a_readable_marker():
    profile = Profile(body_lines=("Part of #6002.",))
    title, body = pull_request_text(
        {"pr_title": "  Prove   the bound ", "pr_body": "x" * 70000}, TASK, profile=profile,
        repository="o/r", spec=AgentSpec("codex", "gpt"), login="me", fields={"task": TASK["article_id"]},
    )
    assert title == "Prove the bound"
    assert len(body) <= 60000 and f"Tasks: {TASK['article_id']}" in body and "Part of #6002." in body
    assert "Autoform project: o/r/blueprint" in body
    assert parse_marker(body) == {"task": TASK["article_id"]}
    assert pull_request_text({}, TASK, profile=profile, repository="o/r", spec=AgentSpec("claude"), login="me",
                             fields={})[0] == "Prove b"


def test_prompts_fill_every_field_or_refuse():
    with pytest.raises(KeyError):
        render("fix_ci.md", {})
    assert slug("SquareSummableComplexInequality") == "square-summable-complex-inequality"
    assert slug("***") == "task"


@posix_only
def test_triage_scores_are_parsed_cached_and_bad_answers_are_unscored(tmp_path):
    from autoform_cli.work_loop.agents import Usage
    from autoform_cli.work_loop.worker import Settings, Worker

    class Runner:
        answers = ['Sure: {"score": 5, "reason": "routine"}', "no idea", '{"score": 9}']

        def run(self, spec, prompt, **options):
            assert "Lean" in prompt or "lean" in prompt
            return Usage(backend=spec.backend, final_text=self.answers.pop(0), cost_usd=0.01)

        def stop(self):
            pass

    settings = Settings(repository="o/r", checkout=tmp_path, base_branch="main", push_repository="me/r",
                        claims_repo=str(tmp_path / "board"), login="me", author=("Me", "me@example.invalid"),
                        plugin_root=tmp_path, agent=AgentSpec("claude"), worker_id="w1", run_id="r1",
                        branch_prefix="autoform-work/", triage=AgentSpec("codex", "small", "low"))
    worker = Worker(settings, Profile(), State(tmp_path / "state", worker="w1", run="r1"), github=None,
                    runner=Runner())
    worker.worktree = tmp_path
    scores = [worker.rate(dict(TASK, article_id=f"af_{index:024x}")) for index in range(3)]
    assert scores == [5, None, None]
    assert worker.triage_scores() == {f"af_{0:024x}": 5}
