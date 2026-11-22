"""Unit tests of the work loop's decisions: profiles, agents, pull-request state, tasks, verification, Git guards."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from autoform_cli.__main__ import _expand_work_shorthand
from tests.lean_fixture import _real_lean_project, _requires_real_lean
from autoform_cli.work_loop import gitops, prstate, tasks, verify
from autoform_cli.work_loop.agents import (
    AgentRunner,
    AgentSpec,
    Price,
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


def test_child_workers_get_the_run_but_not_the_worker_count():
    argv = ["work", "run", "--loop", "--workers", "3", "--agent", "codex", "--workers=4"]
    assert _child_argv(argv, "r1") == ["work", "run", "--loop", "--agent", "codex", "--run-id", "r1"]


@posix_only
def test_each_worker_process_holds_its_own_slot_until_it_exits(tmp_path):
    code = textwrap.dedent(f"""\
        import sys, time
        from pathlib import Path
        from autoform_cli.work_loop.state import State
        print(State(Path({str(tmp_path)!r})).hold_slot(), flush=True)
        time.sleep(float(sys.argv[1]))
        """)
    first = subprocess.Popen([sys.executable, "-c", code, "30"], stdout=subprocess.PIPE, text=True)
    assert first.stdout.readline().strip() == "1"
    second = subprocess.run([sys.executable, "-c", code, "0"], capture_output=True, text=True, check=True)
    assert second.stdout.strip() == "2"
    first.kill()
    first.wait()
    third = subprocess.run([sys.executable, "-c", code, "0"], capture_output=True, text=True, check=True)
    assert third.stdout.strip() == "1"


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


def test_the_review_bot_s_own_checks_are_not_repaired_but_must_pass_to_merge():
    approved = {"reviews": [bot_review("APPROVED")]}
    failed = pr(checks=[check("build"), check("codex-verdict", "FAILURE")], **approved)
    assert decide(failed).kind == "wait"
    missing = pr(checks=[check("build")], **approved)
    assert decide(missing, required={"build", "codex-verdict"}).kind == "wait"
    assert decide(pr(checks=[check("build"), check("codex-verdict")], **approved),
                  required={"build", "codex-verdict"}).kind == "ready"
    assert decide(pr(checks=[check("build")], checks_complete=False, **approved)).kind == "wait"


def test_the_verdict_on_the_head_counts_whatever_reviews_older_commits_got_later():
    reviews = [bot_review("CHANGES_REQUESTED", at="2026-01-01T01:00:00Z", rid="r-head"),
               bot_review("APPROVED", commit="h0", at="2026-01-01T02:00:00Z", rid="r-old")]
    action = decide(pr(reviews=reviews))
    assert action.kind == "fix-review" and action.data["id"] == "r-head"


def test_a_reviewer_with_a_user_account_is_never_taken_for_a_person():
    note = {"id": "c1", "kind": "issue", "author": "review-helper", "bot": False, "created_at": "2026-01-01T03:00:00Z",
            "body": "Automated review: looks fine."}
    value = pr(comments=[note], reviews=[bot_review("APPROVED", marker="")])
    value["reviews"][0]["author"] = "review-helper"
    assert decide(value, review_bot="review-helper", review_markers=[], review_checks=[]).kind == "ready"


def test_a_check_red_on_the_base_is_matched_by_its_own_name_not_by_its_label():
    value = pr(checks=[check("ci / build", "FAILURE", workflow="CI")], reviews=[bot_review("APPROVED")])
    assert decide(value, base_failures={"ci / build"}).kind == "wait"
    assert decide(value, base_failures={"build"}).kind == "fix-ci"


def test_merge_ready_waits_for_a_first_check_and_for_a_queued_rerun():
    assert decide(pr(checks=[]), required=None, review_bot=None).kind == "wait"
    rerun = pr(checks=[check("build"), check("build", "", status="QUEUED", started="")])
    action = decide(rerun, required=None, review_bot=None)
    assert action.kind == "wait" and "CI / build" in action.reason


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
        public axiom open_one : False
        @[simp] public meta axiom hidden : False
        theorem w : True := by exact (axiom_of_choice_free : True)
        """)
    hits = verify.forbidden_hits(text, range(1, 14), verify.FORBIDDEN)
    assert [number for number, _ in hits] == [6, 7, 8, 9, 11, 12]
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
    # Coloured diffs and an external diff driver would hide every added line from a careless parser.
    git(tmp_path, "config", "color.ui", "always")
    git(tmp_path, "config", "diff.external", "false")
    (tmp_path / "b.lean").write_text("-- a comment with sorry\ntheorem x : True := by sorry\n")
    profile = Profile(verify_commands=((sys.executable, "-c", "raise SystemExit(3)"),))
    result = verify.run_steps(tmp_path, base=base, profile=profile, env=dict(os.environ), targets=None, phase=None)
    failed = [step["step"] for step in result.failures()]
    assert failed == ["no proof holes or escapes in added Lean lines"], result.steps
    # Git quotes a path like this one unless told not to; the scan must read it all the same.
    (tmp_path / "b.lean").write_text("theorem x : True := trivial\n")
    (tmp_path / "\u03a9.lean").write_text("theorem y : True := by sorry\n")
    result = verify.run_steps(tmp_path, base=base, profile=profile, env=dict(os.environ), targets=None, phase=None)
    assert "\u03a9.lean:1:" in result.failures()[0]["detail"], result.steps
    (tmp_path / "\u03a9.lean").unlink()
    (tmp_path / "b.lean").write_text("theorem x : True := trivial\n")
    result = verify.run_steps(tmp_path, base=base, profile=profile, env=dict(os.environ), targets=None, phase=None)
    assert [step["step"] for step in result.failures()] == [" ".join(profile.verify_commands[0])]
    # A check that writes a file leaves a change nobody verified.
    writer = Profile(verify_commands=((sys.executable, "-c", "open('Generated.lean', 'w').write('axiom g : False')"),))
    result = verify.run_steps(tmp_path, base=base, profile=writer, env=dict(os.environ), targets=None, phase=None)
    assert [step["step"] for step in result.failures()] == ["the checks left the change as it was verified"]
    assert "Generated.lean" in result.failures()[0]["detail"]


@posix_only
def test_a_deleted_module_s_importers_are_found_under_src_dir_in_every_import_form(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "lakefile.toml").write_text('name = "p"\n\n[[lean_lib]]\nname = "Foo"\nsrcDir = "src"\n')
    (tmp_path / "src" / "Foo").mkdir(parents=True)
    (tmp_path / "src" / "Foo" / "Bar.lean").write_text("def bar := 1\n")
    (tmp_path / "src" / "Foo" / "Baz.lean").write_text("module\npublic meta import all Foo.Bar\n")
    (tmp_path / "src" / "Foo" / "Qux.lean").write_text("import Foo.BarBaz\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "src" / "Foo" / "Bar.lean").unlink()
    result = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    step = next(step for step in result.steps if step["step"] == "nothing imports a deleted module")
    assert not step["ok"] and step["detail"] == "src/Foo/Baz.lean imports the deleted Foo.Bar"


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
    # No tag rides along, even when the user's configuration says so.
    git(work, "config", "push.followTags", "true")
    git(work, "tag", "-a", "-m", "a tag", "sample", second)
    gitops.safe_push(work, commit_sha=second, branch="loop/x", old=first, **options)
    assert git(remote, "rev-parse", "refs/heads/loop/x") == second
    assert git(remote, "for-each-ref", "--format=%(refname)", "refs/heads/main", "refs/tags") == ""
    # An update only adds commits: a head that does not descend from the old one is refused.
    git(work, "checkout", "-q", "--orphan", "other")
    git(work, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "unrelated")
    with pytest.raises(gitops.GitError, match="does not descend"):
        gitops.safe_push(work, commit_sha=git(work, "rev-parse", "HEAD"), branch="loop/x", old=second, **options)


@posix_only
def test_a_worktree_copies_the_checkout_s_packages_and_keeps_its_build_across_resets(tmp_path):
    checkout = tmp_path / "checkout"
    git(tmp_path, "init", "-q", "-b", "main", str(checkout))
    (checkout / "lake-manifest.json").write_text('{"packages": []}\n')
    (checkout / ".gitignore").write_text(".lake\n")
    git(checkout, "add", "-A")
    git(checkout, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "one")
    (checkout / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    (checkout / ".lake" / "packages" / "mathlib" / "rev").write_text("C1")
    (checkout / ".lake" / "build").mkdir()
    (checkout / ".lake" / "build" / "A.olean").write_text("olean")
    worktree = gitops.ensure_worktree(checkout, tmp_path / "wt", "HEAD")
    done = gitops.prepare_lake(worktree, checkout)
    packages = worktree / ".lake" / "packages"
    assert not packages.is_symlink() and (packages / "mathlib" / "rev").read_text() == "C1" and len(done) == 2
    # The worker's Lake can change its copy; the checkout's packages stay as they were.
    (packages / "mathlib" / "rev").write_text("C2")
    assert (checkout / ".lake" / "packages" / "mathlib" / "rev").read_text() == "C1"
    assert gitops.prepare_lake(worktree, checkout) == []
    (worktree / ".lake" / "build" / "B.olean").write_text("mine")
    (worktree / "junk.txt").write_text("x")
    gitops.reset_worktree(worktree, git(checkout, "rev-parse", "HEAD"))
    assert not (worktree / "junk.txt").exists() and (worktree / ".lake" / "build" / "B.olean").exists()
    assert not (checkout / ".lake" / "build" / "B.olean").exists()
    (worktree / "lake-manifest.json").write_text('{"packages": ["new"]}\n')
    assert gitops.prepare_lake(worktree, checkout) == []  # a changed manifest keeps the copy for Lake to update
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


ARTICLE_ID = "af_0123456789abcdef01234567"


def make_worker(tmp_path, *, runner=None, github=None, profile=None, **settings):
    from autoform_cli.work_loop.worker import Settings, Worker

    options = {"repository": "o/r", "checkout": tmp_path, "base_branch": "main", "push_repository": "me/r",
               "claims_repo": str(tmp_path / "board"), "login": "me", "author": ("Me", "me@example.invalid"),
               "plugin_root": tmp_path, "agent": AgentSpec("claude"), "worker_id": "w1", "run_id": "r1",
               "branch_prefix": "autoform-work/", **settings}
    worker = Worker(Settings(**options), profile or Profile(), State(tmp_path / "state", worker="w1", run="r1"),
                    github=github, runner=runner)
    worker.worktree = tmp_path
    return worker


@posix_only
def test_triage_scores_are_parsed_cached_and_bad_answers_are_unscored(tmp_path):
    from autoform_cli.work_loop.agents import Usage

    class Runner:
        answers = ['Sure: {"score": 5, "reason": "routine"}', "no idea", '{"score": 9}']

        def run(self, spec, prompt, **options):
            assert "Lean" in prompt or "lean" in prompt
            return Usage(backend=spec.backend, final_text=self.answers.pop(0), cost_usd=0.01)

        def stop(self):
            pass

    worker = make_worker(tmp_path, runner=Runner(), triage=AgentSpec("codex", "small", "low"))
    scores = [worker.rate(dict(TASK, article_id=f"af_{index:024x}")) for index in range(3)]
    assert scores == [5, None, None]
    assert worker.triage_scores() == {f"af_{0:024x}": 5}


@posix_only
def test_a_parked_pull_request_s_comment_never_quotes_local_detail_and_is_posted_once(tmp_path):
    comments = []

    class GitHubStub:
        def comment(self, repository, number, body):
            comments.append(body)

    worker = make_worker(tmp_path, github=GitHubStub())
    worker.park({"number": 5, "head": "h"}, "the base does not build: /home/someone/Proj/X.lean:3: error")
    assert len(comments) == 1 and "/home/someone" not in comments[0] and "<!-- autoform-work" in comments[0]
    assert "/home/someone" in worker.state.pr_record(5)["parked"]
    # A notice whose response was lost is on the pull request already: it is not posted again.
    posted = {"number": 6, "head": "h", "comments": [{"author": "me", "body": comments[0]}]}
    worker.park(posted, "again")
    assert len(comments) == 1 and worker.state.pr_record(6)["parked"] == "again"


# ---------------------------------------------------------------- the statement probe, on real Lean

_WANTED_BASE = """\
structure ProofWanted (p : Prop) : Prop where
  intro ::

private def helper : Nat → Nat
  | 0 => 1
  | k + 1 => k

/-- A `theorem_wanted` placeholder: a private definition of `ProofWanted T`. -/
private def Ns.swap (n : Nat) : ProofWanted (∀ p : Nat × Nat, (fun (a, b) => b + a + helper n) p = p.2 + p.1 + helper n) :=
  ⟨⟩
"""

_PROVED = """\
import Probe.Wanted

private def helper : Nat → Nat
  | 0 => {zero}
  | k + 1 => k

theorem Ns.swap (n : Nat) : ∀ p : Nat × Nat, (fun (a, b) => b + a + helper n) p = p.2 + p.1 + helper n := by
  intro p; cases p; rfl
"""


@_requires_real_lean
def test_the_probe_accepts_a_faithful_proof_moved_to_another_module_and_refuses_a_changed_definition(tmp_path):
    root = _real_lean_project(tmp_path / "project", "import Probe.Wanted\n", Wanted=_WANTED_BASE)
    env = dict(os.environ)

    def build(*modules):
        built = subprocess.run(["lake", "build", *modules], cwd=root, capture_output=True, text=True, timeout=900)
        assert built.returncode == 0, built.stdout + built.stderr

    build("Probe.Wanted")
    base = verify.statement_facts(root, ["Probe.Wanted"], ["Ns.swap"], env=env)
    assert base["Ns.swap"]["wanted"] and base["Ns.swap"]["constant"].startswith("_private.")
    # The proof moves the statement, its generated matcher and the private helper to another module.
    (root / "Probe" / "Wanted.lean").write_text(_WANTED_BASE.split("private def helper")[0], encoding="utf-8")
    (root / "Probe" / "Proved.lean").write_text(_PROVED.format(zero=1), encoding="utf-8")
    build("Probe.Proved")
    head = verify.statement_facts(root, ["Probe.Proved"], ["Ns.swap"], env=env)
    assert head["Ns.swap"]["kind"] == "theorem" and not head["Ns.swap"]["wanted"]
    assert verify.fidelity_problems(base, head, phase="proof") == []
    # A helper that says something else is a different statement.
    (root / "Probe" / "Proved.lean").write_text(_PROVED.format(zero=2), encoding="utf-8")
    build("Probe.Proved")
    changed = verify.statement_facts(root, ["Probe.Proved"], ["Ns.swap"], env=env)
    problems = verify.fidelity_problems(base, changed, phase="proof")
    assert any("helper changed" in problem for problem in problems), problems


# ---------------------------------------------------------------- worker bookkeeping


@posix_only
def test_a_worker_tends_only_its_operator_s_loop_pull_requests_on_its_push_branches(tmp_path):
    worker = make_worker(tmp_path)
    mine = {"author": "Me", "body": 'x <!-- autoform-work {"task": "af_1", "v": 1} -->', "base": "main",
            "head_repository": "ME/r", "branch": "autoform-work/a"}
    assert worker.owns(mine)
    for change in ({"author": "other"}, {"body": "no marker"}, {"body": "Docs mention `<!-- autoform-work` here."},
                   {"base": "dev"}, {"head_repository": "other/r"}, {"branch": "feature/a"}):
        assert not worker.owns({**mine, **change}), change


@posix_only
def test_a_failed_release_is_kept_and_retried_until_the_claim_is_gone(tmp_path):
    from autoform_cli.claims import ClaimTransportError

    class Board:
        def __init__(self, fail):
            self.fail, self.released = fail, []

        def release(self, key):
            if self.fail:
                raise ClaimTransportError("offline")
            self.released.append(key)
            return True

    worker = make_worker(tmp_path)
    worker.board = Board(fail=True)
    assert worker.release(ARTICLE_ID, "merge-ready", pr=7) is False
    assert len(list((worker.state.root / "releases").glob("*.json"))) == 1
    assert not worker.state.pr_record(7).get("claim_released")
    worker.board = Board(fail=False)
    worker.retry_releases()
    assert worker.board.released and not list((worker.state.root / "releases").glob("*.json"))
    assert worker.state.pr_record(7)["claim_released"] is True


@posix_only
def test_a_run_whose_cost_is_unknown_counts_as_its_cap_or_stops_a_budgeted_loop(tmp_path):
    from autoform_cli.work_loop.agents import Usage

    capped = make_worker(tmp_path / "capped", budget=10.0, attempt_budget=4.0)
    capped.account(Usage(backend="claude", cost_usd=None), "author", cap=4.0)
    assert capped.state.spent(run="r1") == 4.0 and not capped.stopping.is_set()
    uncapped = make_worker(tmp_path / "uncapped", budget=10.0)
    uncapped.runner = type("R", (), {"stop": lambda self: None})()
    uncapped.account(Usage(backend="claude", cost_usd=None), "author")
    assert uncapped.stopping.is_set() and "unknown" in uncapped.stop_reason


@posix_only
def test_reads_are_retried_and_writes_are_not(tmp_path, monkeypatch):
    from autoform_cli.work_loop import github as github_module

    counter = tmp_path / "calls"
    fake = tmp_path / "gh"
    fake.write_text(f"#!/bin/sh\necho x >> {counter}\necho 'HTTP 502: Bad Gateway' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(github_module.time, "sleep", lambda seconds: None)
    client = github_module.GitHub(str(fake), env={}, retries=2)
    with pytest.raises(github_module.GitHubError):
        client.api("repos/o/r/issues/1/comments", method="POST", body={"body": "hi"})
    assert counter.read_text().count("x") == 1
    with pytest.raises(github_module.GitHubError):
        client.api("repos/o/r")
    assert counter.read_text().count("x") == 1 + 3



@posix_only
def test_a_dry_run_records_no_closed_pull_request(tmp_path):
    class GitHubStub:
        def open_pull_requests_by(self, repository, login):
            return []

        def pull_request(self, repository, number):
            return {"state": "CLOSED", "merged_at": None}

    for dry_run, closed in ((True, None), (False, "closed")):
        worker = make_worker(tmp_path / str(dry_run), github=GitHubStub(), dry_run=dry_run)
        worker.board = type("Board", (), {"release": lambda self, key: True})()
        worker.state.update_pr(9, claim=ARTICLE_ID, worker="w1")
        assert worker.loop_pull_requests() == []
        assert worker.state.pr_record(9).get("closed") == closed


def test_a_required_check_counts_only_from_its_producer_and_at_the_head():
    approved = {"reviews": [bot_review("APPROVED")]}
    built = dict(check("build"), app=202)
    assert decide(pr(checks=[built], **approved), required={("build", 101)}).kind == "wait"
    assert decide(pr(checks=[dict(built, app=101)], **approved), required={("build", 101)}).kind == "ready"
    assert decide(pr(checks=[built], **approved), required={("build", None)}).kind == "ready"
    assert decide(pr(checks=[built], checks_commit="h0", **approved), required={"build"}).kind == "wait"


def test_a_codex_run_that_reported_no_turn_has_an_unknown_cost(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "thread.started", "thread_id": "t1"}) + "\n")
    assert parse_usage("codex", transcript, Price(1, 0.1, 2)).cost_usd is None


@posix_only
def test_an_unknown_cost_stops_every_worker_of_the_run_for_good(tmp_path):
    from autoform_cli.work_loop.agents import Usage

    first = make_worker(tmp_path, budget=10.0)
    first.runner = type("R", (), {"stop": lambda self: None})()
    first.account(Usage(backend="claude", cost_usd=None), "author")
    later = make_worker(tmp_path, budget=10.0)
    assert "unknown" in later.should_stop()


@posix_only
def test_a_withheld_reply_is_not_an_answer_and_a_delivered_one_is_not_posted_twice(tmp_path):
    comments = []

    class GitHubStub:
        def comment(self, repository, number, body):
            comments.append(body)

    refusing = make_worker(tmp_path / "a", github=GitHubStub(),
                           profile=Profile(publish_check=(sys.executable, "-c", "raise SystemExit(1)")))
    assert refusing.reply({"number": 1, "head": "h", "comments": []}, "Done.", to=["c1"]) is False
    assert comments == []
    worker = make_worker(tmp_path / "b", github=GitHubStub())
    assert worker.reply({"number": 1, "head": "h", "comments": []}, "Done.", to=["c1"]) is True
    delivered = {"number": 1, "head": "h", "comments": [{"author": "me", "body": comments[0]}]}
    assert worker.reply(delivered, "Done.", to=["c1"]) is True and len(comments) == 1


@posix_only
def test_protected_branches_are_read_again_before_each_push(tmp_path):
    reads = []

    class GitHubStub:
        def repository(self, name):
            reads.append(name)
            return {"default_branch": "trunk" if len(reads) == 1 else "autoform-work/x"}

    worker = make_worker(tmp_path, github=GitHubStub())
    assert "trunk" in worker.protected()
    assert "autoform-work/x" in worker.protected() and len(reads) == 2


@posix_only
def test_the_picker_runs_from_its_whole_directory_at_the_base(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "picker.py").write_text("import json, pathlib\n"
                                                    "DATA = json.loads((pathlib.Path(__file__).parent / 'data.json').read_text())\n"
                                                    "def taken(items, pulls):\n    return set(DATA)\n")
    (tmp_path / "scripts" / "data.json").write_text('["af_x"]')
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    (tmp_path / "scripts" / "picker.py").write_text("raise SystemExit('edited')\n")
    root = gitops.export_tree(tmp_path, "HEAD", "scripts", tmp_path / "out")
    taken = tasks.load_taken_function(root, "scripts/picker.py")
    assert taken([], []) == {"af_x"}


@posix_only
def test_a_mode_only_change_has_no_added_lines(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "A.lean").write_text("theorem a : True := trivial\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    (tmp_path / "A.lean").chmod(0o755)
    git(tmp_path, "add", "-A")
    assert gitops.added_lines(tmp_path, "HEAD", "A.lean") == set()


@posix_only
def test_the_agent_s_git_calls_no_credential_helper_the_checkout_configures(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    called = tmp_path / "helper-called"
    git(tmp_path, "config", "credential.helper", f"!touch {called}; true")
    env = agent_env({"PATH": os.environ["PATH"], "HOME": str(tmp_path)}, scratch=tmp_path / "scratch")
    subprocess.run(["git", "credential", "fill"], cwd=tmp_path, env=env, input="protocol=https\nhost=example.com\n\n",
                   capture_output=True, text=True, timeout=60, check=False)
    assert not called.exists()
    subprocess.run(["git", "credential", "fill"], cwd=tmp_path, input="protocol=https\nhost=example.com\n\n",
                   env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"},
                   capture_output=True, text=True, timeout=60, check=False)
    assert called.exists()  # the helper is real: without the agent's environment it runs


@posix_only
def test_the_commit_holds_exactly_the_verified_tree(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "note.txt").write_text("checked\n")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert check.ok and check.tree
    (tmp_path / "Late.lean").write_text("axiom late : False\n")  # appears after verification
    sha = gitops.commit(tmp_path, "Add a note", author=("T", "t@example.invalid"), tree=check.tree)
    assert git(tmp_path, "ls-tree", "--name-only", sha).split() == ["a.txt", "note.txt"]


@posix_only
def test_a_no_diff_attribute_does_not_hide_added_lean_lines(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / ".gitattributes").write_text("*.lean -diff\n")
    (tmp_path / "A.lean").write_text("theorem a : True := trivial\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "A.lean").write_text("theorem a : True := trivial\ntheorem b : False := by sorry\n")
    result = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert [step["step"] for step in result.failures()] == ["no proof holes or escapes in added Lean lines"]


@posix_only
def test_url_scoped_credential_settings_of_the_checkout_are_emptied_for_agents(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "http.https://example.com/.extraHeader", "X-Fixture: 1")
    git(tmp_path, "config", "credential.https://example.com.helper", "store")
    env = agent_env({"PATH": os.environ["PATH"], "HOME": str(tmp_path)}, scratch=tmp_path / "s", checkout=tmp_path)

    def urlmatch(key: str, environment) -> str:
        return subprocess.run(["git", "config", "--get-urlmatch", key, "https://example.com/repo"], cwd=tmp_path,
                              env=environment, capture_output=True, text=True, check=False).stdout.strip()

    plain = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    assert urlmatch("http.extraheader", plain) == "X-Fixture: 1" and urlmatch("credential.helper", plain) == "store"
    assert urlmatch("http.extraheader", env) == "" and urlmatch("credential.helper", env) == ""


@posix_only
def test_the_picker_export_ignores_archive_attributes(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "picker.py").write_text("x = 1\n")
    (tmp_path / "scripts" / "data.json").write_text("[]")
    (tmp_path / ".gitattributes").write_text("scripts/data.json export-ignore\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    out = gitops.export_tree(tmp_path, "HEAD", "scripts", tmp_path / "out")
    assert sorted(path.name for path in (out / "scripts").iterdir()) == ["data.json", "picker.py"]


@posix_only
def test_a_parking_notice_that_fails_to_post_is_retried_not_forgotten(tmp_path):
    from autoform_cli.work_loop.github import GitHubError

    class FlakyGitHub:
        calls = 0

        def comment(self, repository, number, body):
            FlakyGitHub.calls += 1
            if FlakyGitHub.calls == 1:
                raise GitHubError("HTTP 502")

    worker = make_worker(tmp_path, github=FlakyGitHub())
    with pytest.raises(GitHubError):
        worker.park({"number": 4, "head": "h"}, "why", public="a reason")
    record = worker.state.pr_record(4)
    assert record.get("pending_park") == "why" and not record.get("parked")
    worker.park({"number": 4, "head": "h"}, "why", public="a reason")
    record = worker.state.pr_record(4)
    assert record.get("parked") == "why" and not record.get("pending_park") and FlakyGitHub.calls == 2


@posix_only
def test_another_origin_s_pending_branch_and_release_are_left_alone(tmp_path):
    class GitHubStub:
        def find_pull_request(self, repository, head):
            raise AssertionError("looked up another origin's branch")

    worker = make_worker(tmp_path, github=GitHubStub())
    other = {"branch": "autoform-work/x", "task": "af_1", "claim": "af_1", "head": "h", "worker": "them-w1",
             "at": 0, **worker.origin(), "login": "someone-else"}
    worker.state.write_json(worker.state.root / "pending" / "x.json", other)
    worker.state.write_json(worker.state.root / "releases" / "y.json", {**other, "target": "af_1", "owner": "them-w1"})
    worker.board = type("Board", (), {"release": lambda self, key: (_ for _ in ()).throw(AssertionError(key))})()
    worker.recover_pending()
    worker.retry_releases()
    assert (worker.state.root / "pending" / "x.json").exists() and (worker.state.root / "releases" / "y.json").exists()
    assert worker.pending_records() == [] and len(worker.pending_records(everyone=True)) == 1


@posix_only
def test_no_agent_is_dispatched_once_the_run_must_stop_and_a_codex_cost_is_never_guessed(tmp_path):
    from autoform_cli.work_loop.agents import Usage

    class Runner:
        runs = 0

        def run(self, spec, prompt, **options):
            Runner.runs += 1
            return Usage(backend=spec.backend, cost_usd=None)

        def stop(self):
            pass

    worker = make_worker(tmp_path, runner=Runner(), budget=10.0, attempt_budget=0.5, agent=AgentSpec("codex"))
    worker.run_agent(AgentSpec("codex"), "go", label="repair")
    assert Runner.runs == 1 and worker.stopping.is_set() and worker.state.spent(run="r1") == 0
    peer = make_worker(tmp_path, runner=Runner(), budget=10.0)
    with pytest.raises(Stopped):
        peer.run_agent(AgentSpec("claude"), "go", label="repair")
    assert Runner.runs == 1


def test_checks_of_two_producers_both_count_and_stale_checks_spend_no_repair():
    approved = {"reviews": [bot_review("APPROVED")]}
    failed = dict(check("build", "FAILURE", workflow=""), app=202)
    passed = dict(check("build", started="2026-01-01T05:00:00Z", workflow=""), app=101)
    assert decide(pr(checks=[failed, passed], **approved), required={("build", 101)}).kind == "fix-ci"
    stale = pr(checks=[check("build", "FAILURE")], checks_commit="h0", **approved)
    assert decide(stale).kind == "wait"


@posix_only
def test_the_attempt_limit_stops_authoring_not_the_reconciliation_of_a_pushed_branch(tmp_path):
    class GitHubStub:
        def open_pull_requests_by(self, repository, login):
            return []

        def required_checks(self, repository, branch):
            return frozenset()

        def failed_checks_on(self, repository, ref):
            return frozenset()

    worker = make_worker(tmp_path, github=GitHubStub(), max_attempts=1, loop=True)
    worker.state.event("author-start", task="af_1")
    pending = {"branch": "autoform-work/x", "task": "af_1", "claim": "af_1", "head": "h", "worker": "w1",
               "at": time.time(), **worker.origin()}
    worker.state.write_json(worker.state.root / "pending" / "x.json", pending)
    assert worker.round() == {"phase": "idle", "outcome": "following", "open": 0, "pending": 1}
    (worker.state.root / "pending" / "x.json").unlink()
    assert worker.round()["outcome"] == "done"


@posix_only
def test_a_save_during_verification_cannot_slip_an_unchecked_file_into_the_commit(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "Note.md").write_text("note\n")
    original = gitops.changed_files

    def save_meanwhile(path, base_, tree=None):  # a file lands right after the snapshot is taken
        rows = original(path, base_, tree)
        (tmp_path / "Generated.lean").write_text("theorem g : False := by sorry\n")
        return rows

    verify_module = verify
    patched = verify_module.changed_files
    verify_module.changed_files = save_meanwhile
    try:
        check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    finally:
        verify_module.changed_files = patched
    assert not check.ok and check.failures()[-1]["step"] == "the checks left the change as it was verified"
    assert "Generated.lean" not in git(tmp_path, "ls-tree", "-r", "--name-only", check.tree)


@posix_only
def test_a_new_empty_lean_module_has_no_added_lines(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "Empty.lean").write_text("")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert check.ok, check.steps


@posix_only
def test_a_release_keeps_to_the_board_and_origin_it_was_made_on(tmp_path):
    from autoform_cli.claims import ClaimTransportError

    worker = make_worker(tmp_path)
    boards = {}

    class Board:
        def __init__(self, repository, fail):
            self.repository, self.fail = repository, fail

        def release(self, key):
            if self.fail:
                raise ClaimTransportError("offline")
            return True

    worker.board_for = lambda owner, repository=None: boards[repository or worker.s.claims_repo]
    worker.board = Board(worker.s.claims_repo, fail=False)
    boards["board-a"] = Board("board-a", fail=True)
    boards[worker.s.claims_repo] = worker.board
    assert worker.release(ARTICLE_ID, "closed", owner="w1", pr=3, board_repository="board-a") is False
    assert worker.release(ARTICLE_ID, "closed", pr=4) is True
    left = [worker.state.read_json(path) for path in (worker.state.root / "releases").glob("*.json")]
    assert len(left) == 1 and left[0]["board"] == "board-a"


@posix_only
def test_a_reply_already_given_to_one_comment_is_not_given_again_with_a_new_one(tmp_path):
    worker = make_worker(tmp_path)
    pr_value = {"comments": [{"author": "me", "body": 'Done.\n\n<!-- autoform-work {"reply": true, "to": ["issue:9"]} -->'}]}
    assert worker.answered_ids(pr_value) == {"issue:9"}


@posix_only
def test_the_agent_environment_reads_the_worker_worktree_s_own_configuration(tmp_path):
    checkout = tmp_path / "checkout"
    git(tmp_path, "init", "-q", "-b", "main", str(checkout))
    (checkout / "f").write_text("1\n")
    git(checkout, "add", "-A")
    git(checkout, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "one")
    git(checkout, "config", "extensions.worktreeConfig", "true")
    worktree = gitops.ensure_worktree(checkout, tmp_path / "wt", "HEAD")
    git(worktree, "config", "--worktree", "http.https://example.com/.extraHeader", "X-Fixture: 1")
    env = agent_env({"PATH": os.environ["PATH"], "HOME": str(tmp_path)}, scratch=tmp_path / "s",
                    checkout=[checkout, worktree])
    found = subprocess.run(["git", "config", "--get-urlmatch", "http.extraheader", "https://example.com/r"],
                           cwd=worktree, env=env, capture_output=True, text=True, check=False).stdout.strip()
    assert found == ""
    with pytest.raises(gitops.GitError):
        agent_env({"PATH": os.environ["PATH"]}, scratch=tmp_path / "s", checkout=tmp_path / "missing")


@posix_only
def test_a_lean_file_that_is_a_symbolic_link_is_refused(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "Sample.lean.in").write_text("theorem sample : True := by sorry\n")
    (tmp_path / "Sample.lean").symlink_to("Sample.lean.in")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert [step["step"] for step in check.failures()] == ["no proof holes or escapes in added Lean lines"]
    assert "symbolic link" in check.failures()[0]["detail"]


@posix_only
def test_a_lean_source_that_is_not_utf8_is_unreadable_not_guessed(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "Bad.lean").write_bytes(b"-- caf\xe9\ntheorem b : True := trivial\n")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert "not UTF-8" in check.failures()[0]["detail"]


@posix_only
def test_a_change_to_the_file_a_lean_link_points_to_is_refused(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "Sample.lean.in").write_text("theorem sample : True := trivial\n")
    (tmp_path / "Sample.lean").symlink_to("Sample.lean.in")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "Sample.lean.in").write_text("theorem sample : True := by sorry\n")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert "a symbolic link to the changed Sample.lean.in" in check.failures()[0]["detail"]


@posix_only
@pytest.mark.parametrize("layout", ["chain", "through-directory", "directory", "unrelated"])
def test_a_change_reached_through_a_chain_of_links_or_a_linked_directory_is_refused(tmp_path, layout):
    git(tmp_path, "init", "-q", "-b", "main")
    if layout in ("chain", "unrelated"):
        (tmp_path / "Sample.lean.in").write_text("theorem sample : True := trivial\n")
        (tmp_path / "current").symlink_to("Sample.lean.in")
        (tmp_path / "Sample.lean").symlink_to("current")
        (tmp_path / "README.md").write_text("readme\n")
        edited = tmp_path / ("Sample.lean.in" if layout == "chain" else "README.md")
    elif layout == "through-directory":
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "Sample.lean.in").write_text("theorem sample : True := trivial\n")
        (tmp_path / "current").symlink_to("src")
        (tmp_path / "Sample.lean").symlink_to("current/Sample.lean.in")
        edited = tmp_path / "src" / "Sample.lean.in"
    else:
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "Sample.lean").write_text("theorem sample : True := trivial\n")
        (tmp_path / "Lib").symlink_to("real")
        edited = tmp_path / "real" / "Sample.lean"
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    edited.write_text("theorem sample : True := by sorry\n")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    if layout == "unrelated":  # prose beside links is not refused
        assert check.ok, check.steps
        return
    assert not check.ok
    detail = check.failures()[0]["detail"]
    if layout == "chain":
        assert "Sample.lean: a symbolic link to the changed Sample.lean.in" in detail
    elif layout == "through-directory":
        assert "Sample.lean: a symbolic link to the changed src/Sample.lean.in" in detail
    else:
        assert "Lib: a symbolic link to the changed real" in detail


@posix_only
def test_links_resolve_as_the_filesystem_does_and_documentation_aliases_are_left_alone(tmp_path):
    links = {"Sample.lean": "current", "current": "proofs/hole.lean.in", "d": "x/y", "Up.lean": "d/../z.lean"}
    assert verify._resolve_in_tree(links, "Sample.lean") == ("proofs/hole.lean.in", {"Sample.lean", "current"})
    # `..` after a linked directory leaves its target, not the link's own directory.
    assert verify._resolve_in_tree(links, "Up.lean") == ("x/z.lean", {"Up.lean", "d"})
    assert verify._resolve_in_tree({"a": "b", "b": "a"}, "a") is None
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "proofs").mkdir()
    (tmp_path / "proofs" / "done.lean.in").write_text("theorem s : True := trivial\n")
    (tmp_path / "proofs" / "hole.lean.in").write_text("theorem s : True := by sorry\n")
    (tmp_path / "current").symlink_to("proofs/done.lean.in")
    (tmp_path / "Sample.lean").symlink_to("current")
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("readme\n")
    (tmp_path / "docs" / "intro.md").symlink_to("../README.md")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("more readme\n")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert check.ok, check.steps  # a documentation alias does not stop a prose change
    # Retargeting only the intermediate link makes Lean read the hole.
    (tmp_path / "current").unlink()
    (tmp_path / "current").symlink_to("proofs/hole.lean.in")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert "Sample.lean: a symbolic link to the changed current" in check.failures()[0]["detail"]


@posix_only
def test_prose_under_a_linked_directory_and_a_dot_dot_source_are_told_apart(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "notes.md").write_text("notes\n")
    (tmp_path / "Lib").symlink_to("real")
    (tmp_path / "src" / "inner").mkdir(parents=True)
    (tmp_path / "src" / "impl.in").write_text("theorem i : True := trivial\n")
    (tmp_path / "current").symlink_to("src/inner")
    (tmp_path / "X.lean").symlink_to("current/../impl.in")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "real" / "notes.md").write_text("more notes\n")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert check.ok, check.steps
    (tmp_path / "src" / "impl.in").write_text("theorem i : True := by sorry\n")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert "X.lean: a symbolic link to the changed src/impl.in" in check.failures()[0]["detail"]


@posix_only
@pytest.mark.parametrize("lean", [True, False])
def test_a_retargeted_directory_alias_is_refused_when_it_exposes_lean_files(tmp_path, lean):
    git(tmp_path, "init", "-q", "-b", "main")
    name = "Sample.lean" if lean else "notes.md"
    for folder, body in (("stable", "theorem s : True := trivial\n"), ("next", "theorem s : True := by sorry\n")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / name).write_text(body)
    (tmp_path / "Lib").symlink_to("stable")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "Lib").unlink()
    (tmp_path / "Lib").symlink_to("next")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    if lean:
        assert "Lib: a changed symbolic link to Lean sources" in check.failures()[0]["detail"]
    else:
        assert check.ok, check.steps


@posix_only
def test_a_retargeted_alias_exposing_lean_through_a_nested_directory_link_is_refused(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    for chapter, body in (("a", "theorem s : True := trivial\n"), ("b", "theorem s : True := by sorry\n")):
        (tmp_path / "chapters" / chapter).mkdir(parents=True)
        (tmp_path / "chapters" / chapter / "Sample.lean").write_text(body)
    for folder, chapter in (("stable", "a"), ("next", "b")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "Chapter").symlink_to(f"../chapters/{chapter}")
    (tmp_path / "Lib").symlink_to("stable")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "Lib").unlink()
    (tmp_path / "Lib").symlink_to("next")
    check = verify.run_steps(tmp_path, base=base, profile=Profile(), env=dict(os.environ), targets=None, phase=None)
    assert "Lib: a changed symbolic link to Lean sources" in check.failures()[0]["detail"]
    assert not verify._exposes_lean({"a/b": "120000", "b/a": "120000"}, {"a/b": "../b", "b/a": "../a"}, "a/b")
