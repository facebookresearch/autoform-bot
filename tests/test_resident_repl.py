"""The resident REPL behind `lean_try_tactics`: reuse, fallbacks, and failures."""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from servers.lean_runtime import LeanRuntimeConfig, LeanRuntimeServices
from servers.lsp.interactive import format_tactic_attempts
from servers.repl import resident as rr
from tests.test_shared_lean_runtime import FakePool, make_lake_project, runtime_config

# Speaks the REPL's protocol: JSON requests and responses, each ended by a blank
# line. Every request it reads is appended to $FAKE_REPL_LOG.
_FAKE_REPL = textwrap.dedent(
    """
    import json, os, sys, time
    log = open(os.environ["FAKE_REPL_LOG"], "a")
    states = 0

    def reply(obj):
        sys.stdout.write(json.dumps(obj) + "\\n\\n")
        sys.stdout.flush()

    def frames():
        buf = ""
        for line in sys.stdin:
            if line.strip():
                buf += line
            elif buf:
                yield json.loads(buf)
                buf = ""

    for req in frames():
        kind = "tactic" if "tactic" in req else ("cmd" if "env" in req else "header")
        log.write(kind + " " + json.dumps(req.get("tactic", "")) + "\\n")
        log.flush()
        if kind == "header":
            if "SlowHeader" in req["cmd"]:
                time.sleep(float(os.environ.get("FAKE_SLOW", "2")))
            if "BadImport" in req["cmd"]:
                reply({"messages": [{"severity": "error", "pos": {"line": 1, "column": 0},
                                     "data": "unknown module prefix 'BadImport'"}]})
            else:
                reply({"env": 0})
        elif kind == "cmd":
            if "nested" in req["cmd"]:
                time.sleep(float(os.environ.get("FAKE_CONFIRM_DELAY", "0")))
            messages, sorries = [], []
            for number, text in enumerate(req["cmd"].split("\\n"), start=1):
                if "BROKEN" in text or "nestedbad" in text:
                    messages.append({"severity": "error", "pos": {"line": number, "column": 0},
                                     "data": "unknown identifier 'BROKEN'"})
                if text.strip() == "sorry" and not text.startswith(" "):
                    messages.append({"severity": "error", "pos": {"line": number, "column": 0},
                                     "data": "unexpected token 'sorry'; expected command"})
                elif text.strip() == "sorry" and "NOSORRY" not in req["cmd"]:
                    sorries.append({"pos": {"line": number, "column": 2}, "goal": "⊢ True",
                                    "proofState": states})
                    states += 1
            reply({"env": 1, "messages": messages, "sorries": sorries})
        else:
            tactic = req["tactic"]
            states += 1
            if tactic == "loop":
                time.sleep(60)
            if tactic in ("nested", "nestedbad"):
                # No goals, but the REPL's isolated kernel check rejects the term.
                reply({"proofState": states, "goals": [],
                       "proofStatus": "Error: kernel type check failed: (kernel) application type mismatch"})
            elif tactic in ("omega", "trivial"):
                reply({"proofState": states, "goals": [], "proofStatus": "Completed"})
            elif tactic == "skip":
                reply({"proofState": states, "goals": ["⊢ True"], "proofStatus": "Incomplete: open goals remain"})
            elif tactic == "sorry":
                reply({"proofState": states, "goals": [], "proofStatus": "Incomplete: contains sorry",
                       "messages": [{"severity": "warning", "pos": {"line": 1, "column": 0},
                                     "data": "declaration uses 'sorry'"}]})
            else:
                reply({"message": "Lean error:\\nunknown tactic " + tactic})
    """
)

# Prints what parse_header.lean prints: the import lines it is shown, unless
# FAKE_HEADER_MODULES names others, and `isModule` when the text starts with `module`.
_FAKE_PARSER = textwrap.dedent(
    """
    import json, os, sys
    text = sys.stdin.read()
    with open(os.environ["FAKE_PARSE_LOG"], "a") as log:
        log.write("parse\\n")
    override = os.environ.get("FAKE_HEADER_MODULES")
    if override is not None:
        modules = [name for name in override.split(",") if name]
    else:
        modules = [line.split()[1] for line in text.splitlines() if line.startswith("import ")]
    imports = [{"module": "Init"}] + [{"module": name} for name in modules]
    result = {"imports": imports, "isModule": text.lstrip().startswith("module"), "isPrelude": False}
    print(json.dumps({"imports": [{"errors": [], "result": result}]}))
    """
)

_FILE = "import Probe.Basic\n\n/-! A module doc. -/\n\ntheorem t : True := by\n  sorry\n"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_repl.py"
    script.write_text(_FAKE_REPL, encoding="utf-8")
    parser = tmp_path / "fake_parser.py"
    parser.write_text(_FAKE_PARSER, encoding="utf-8")
    log = tmp_path / "requests.log"
    log.touch()
    monkeypatch.setenv("FAKE_REPL_LOG", str(log))
    monkeypatch.setenv("FAKE_PARSE_LOG", str(tmp_path / "parse.log"))
    project = make_lake_project(tmp_path, "resident")
    source = project / "Probe" / "Basic.lean"
    source.parent.mkdir(parents=True)
    source.write_text("theorem probe : True := trivial\n", encoding="utf-8")
    olean = project / ".lake" / "build" / "lib" / "lean" / "Probe" / "Basic.olean"
    olean.parent.mkdir(parents=True)
    olean.write_bytes(b"olean")
    built = source.stat().st_mtime_ns + 10**9
    os.utime(olean, ns=(built, built))
    main = project / "Main.lean"
    main.write_text(_FILE, encoding="utf-8")
    repl = rr.ResidentRepl(project, [sys.executable, str(script)], header_command=[sys.executable, str(parser)])
    yield repl, main, olean, log
    repl.close()


def _requests(log: Path) -> list[str]:
    return [line.split()[0] for line in log.read_text().splitlines()]


def _try(repl, main, line=5, tactics=("omega",), per=5.0, total=30.0):
    return repl.try_tactics(str(main), line, list(tactics), per_candidate_timeout=per, total_timeout=total)


def test_split_header_stops_at_the_first_command():
    split = rr.split_header("-- c\nimport A.B\n/- block\n -/\nimport C\n\n/-- doc -/\ntheorem t : True := trivial\n")
    assert split.imports == ("A.B", "C")
    assert split.header_lines == 5
    assert split.body_lines[0] == "\n"
    assert rr.split_header("theorem t : True := trivial\n").header_lines == 0


def test_candidates_are_classified_at_the_goal_sorry_leaves(fake):
    repl, main, _, log = fake
    result = _try(repl, main, tactics=("omega", "skip", "sorry", "nope"))
    outcomes = {attempt["tactic"]: attempt["outcome"] for attempt in result["attempts"]}
    assert outcomes == {"omega": "closed", "skip": "error", "sorry": "incomplete", "nope": "error"}
    assert result["backend"] == "repl"
    assert result["replaced"] == "  sorry"
    assert "unknown tactic nope" in result["attempts"][3]["detail"]
    assert main.read_text() == _FILE
    assert _requests(log) == ["header", "cmd", "cmd", "tactic", "tactic", "tactic", "tactic"]


def test_the_header_and_body_are_elaborated_once_across_calls(fake):
    repl, main, _, log = fake
    _try(repl, main)
    _try(repl, main, tactics=("trivial",))
    # An edit to the body reuses the imported header.
    main.write_text(_FILE.replace("theorem t", "theorem t'"), encoding="utf-8")
    _try(repl, main)
    # The declaration changed and the text before it did not.
    assert _requests(log) == ["header", "cmd", "cmd", "tactic", "tactic", "cmd", "tactic"]
    assert repl.restarts == 1


def test_a_rebuilt_import_starts_a_fresh_process(fake):
    repl, main, olean, log = fake
    _try(repl, main)
    stamp = olean.stat().st_mtime_ns + 10**9
    os.utime(olean, ns=(stamp, stamp))
    _try(repl, main)
    assert _requests(log) == ["header", "cmd", "cmd", "tactic", "header", "cmd", "cmd", "tactic"]
    assert repl.restarts == 2


@pytest.mark.parametrize(
    ("text", "line", "reason"),
    [
        (_FILE.replace("/-! A module doc. -/", "#check BROKEN"), 5, "error before this declaration, at line 2: unknown"),
        (_FILE.replace("True := by", "BROKEN := by"), 5, "the declaration has an error at line 4: unknown"),
        ("import Probe.Basic\n\ntheorem t : True :=\n  sorry\n", 3, "term after `:=`"),
        (_FILE, 0, "import header"),
        (_FILE, 3, "blank"),
        ("import BadImport\n\ntheorem t : True := by\n  sorry\n", 3, "could not import the header"),
        (_FILE, 4, "the line starts a command"),
        (_FILE.replace("True := by", "True := by -- NOSORRY"), 5, "no goal for a `sorry`"),
    ],
)
def test_lines_the_fast_path_cannot_answer_for_fall_back(fake, text, line, reason):
    repl, main, _, _ = fake
    main.write_text(text, encoding="utf-8")
    with pytest.raises(rr.ResidentFallback, match=reason):
        _try(repl, main, line=line)


def test_an_inconclusive_kernel_check_is_settled_by_the_declaration(fake):
    repl, main, _, log = fake
    result = _try(repl, main, tactics=("nested", "nestedbad"))
    assert [(a["outcome"], a["detail"]) for a in result["attempts"]] == [
        ("closed", "confirmed by elaborating the declaration"),
        ("error", "unknown identifier 'BROKEN'"),
    ]
    assert _requests(log) == ["header", "cmd", "cmd", "tactic", "cmd", "tactic", "cmd"]


def test_a_confirmation_may_outlast_the_candidate_budget(fake, monkeypatch):
    repl, main, _, _ = fake
    monkeypatch.setenv("FAKE_CONFIRM_DELAY", "1.5")
    result = _try(repl, main, tactics=("nested",), per=0.5, total=30)
    assert result["attempts"][0]["outcome"] == "closed"
    assert repl.restarts == 1


def test_a_closed_goal_in_a_declaration_with_another_sorry_is_incomplete(fake):
    repl, main, _, _ = fake
    main.write_text(_FILE.replace("  sorry\n", "  sorry\n  sorry\n"), encoding="utf-8")
    result = _try(repl, main)
    assert result["attempts"][0] == {
        "tactic": "omega",
        "outcome": "incomplete",
        "detail": "closes this goal; the declaration has another sorry",
    }


def test_declaration_bounds_take_attributes_and_doc_comments():
    lines = ["namespace N\n", "/-- doc\n", "  more -/\n", "@[simp]\n", "theorem t : True := by\n",
             "  trivial\n", "\n", "end N\n"]
    assert rr.declaration_bounds(lines, 5) == (1, 7)
    lines.insert(1, "open Classical in\n")
    assert rr.declaration_bounds(lines, 6) == (1, 8)


def test_an_overrunning_candidate_costs_the_process_but_not_the_batch(fake):
    repl, main, _, log = fake
    result = _try(repl, main, tactics=("loop", "omega"), per=1.0)
    assert [attempt["outcome"] for attempt in result["attempts"]] == ["timeout", "closed"]
    assert _requests(log) == ["header", "cmd", "cmd", "tactic", "header", "cmd", "cmd", "tactic"]
    assert repl.restarts == 2


def test_a_slow_header_keeps_loading_and_the_next_call_collects_it(fake, monkeypatch):
    repl, main, _, log = fake
    monkeypatch.setenv("FAKE_SLOW", "2")
    main.write_text(_FILE.replace("Probe.Basic", "Probe.SlowHeader"), encoding="utf-8")
    with pytest.raises(rr.ResidentStillLoading, match="importing the header"):
        _try(repl, main, total=0.5)
    time.sleep(2)
    result = _try(repl, main)
    assert result["attempts"][0]["outcome"] == "closed"
    assert _requests(log) == ["header", "cmd", "cmd", "tactic"]
    assert repl.restarts == 1


def test_a_repl_that_dies_is_reported_and_replaced(fake, tmp_path):
    repl, main, _, _ = fake
    repl.repl_command = [sys.executable, "-c", "import sys; sys.stderr.write('unknown package repl'); sys.exit(1)"]
    with pytest.raises(rr.ResidentReplError, match="unknown package repl"):
        _try(repl, main)
    assert repl.stats()["running"] is False


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #


class _FakeLsp:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def is_alive(self):
        return True

    def close(self):
        pass

    def retire(self):
        pass

    def try_tactics(self, file_path, line, tactics, owner, *, per_candidate_timeout, total_timeout):
        self.calls.append((line, tactics))
        return {"line": line, "replaced": "#check BROKEN", "attempts": [{"tactic": "rfl", "outcome": "error"}]}


def test_the_runtime_uses_the_resident_repl_and_falls_back_with_a_note(fake):
    repl, main, _, _ = fake
    sessions = []

    def lsp_factory(root):
        sessions.append(_FakeLsp(root))
        return sessions[-1]

    services = LeanRuntimeServices(
        runtime_config(tactic_backend="repl"),
        repl_factory=FakePool,
        lsp_factory=lsp_factory,
        resident_factory=lambda root: repl,
        start_sweepers=False,
    )
    try:
        params = {"project_dir": str(main.parent), "file_path": "Main.lean", "owner": "me", "tactics": ["omega"]}
        fast = services.dispatch("lsp.try_tactics", {**params, "line": 5})
        assert "in the resident REPL" in fast and "closed     omega" in fast
        assert sessions == []
        main.write_text(_FILE.replace("/-! A module doc. -/", "#check BROKEN"), encoding="utf-8")
        slow = services.dispatch("lsp.try_tactics", {**params, "line": 5})
        assert "buffer only" in slow
        assert slow.endswith("Checked with the language server, not the resident REPL: the file has an "
                             "error before this declaration, at line 2: unknown identifier 'BROKEN'.")
        assert sessions[0].calls == [(5, ["omega"])]
        assert services.status(include_projects=True)["resident_repls"]["resident"]
    finally:
        services.close()


def test_the_lsp_backend_never_starts_a_resident_repl(tmp_path):
    project = make_lake_project(tmp_path, "lsp-only")
    (project / "Main.lean").write_text("theorem t : True := by\n  sorry\n")
    created = []
    services = LeanRuntimeServices(
        runtime_config(),
        repl_factory=FakePool,
        lsp_factory=_FakeLsp,
        resident_factory=lambda root: created.append(root),
        start_sweepers=False,
    )
    try:
        services.dispatch(
            "lsp.try_tactics",
            {"project_dir": str(project), "file_path": "Main.lean", "owner": "me", "line": 1, "tactics": ["rfl"]},
        )
    finally:
        services.close()
    assert created == []


def test_the_tactic_backend_is_node_configuration(monkeypatch):
    for name in ("AUTOFORM_TACTIC_BACKEND", "AUTOFORM_RESIDENT_REPL_MAX_GB"):
        monkeypatch.delenv(name, raising=False)
    assert LeanRuntimeConfig.from_environment().tactic_backend == "lsp"
    monkeypatch.setenv("AUTOFORM_TACTIC_BACKEND", "repl")
    monkeypatch.setenv("AUTOFORM_RESIDENT_REPL_MAX_GB", "6")
    config = LeanRuntimeConfig.from_environment()
    assert (config.tactic_backend, config.resident_repl_max_gb) == ("repl", 6.0)
    assert json.dumps(config.as_dict())
    monkeypatch.setenv("AUTOFORM_TACTIC_BACKEND", "both")
    with pytest.raises(ValueError, match="AUTOFORM_TACTIC_BACKEND"):
        LeanRuntimeConfig.from_environment()


@pytest.mark.parametrize("warning", ["declaration uses `sorry`", "declaration uses 'sorry'"])
def test_a_sorry_warning_reads_as_incomplete_in_either_spelling(warning):
    response = {"messages": [{"severity": "warning", "data": warning}]}
    assert rr.classify_declaration(response)["outcome"] == "incomplete"


def _parses(tmp_path: Path) -> int:
    log = tmp_path / "parse.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


def test_the_header_is_checked_once_per_list_of_imports(fake, tmp_path):
    repl, main, _, _ = fake
    _try(repl, main)
    _try(repl, main)
    # A comment, even one inside the header, is not part of the check's key.
    main.write_text(_FILE.replace("A module doc.", "Another module doc."), encoding="utf-8")
    _try(repl, main)
    main.write_text("-- a note\n" + _FILE, encoding="utf-8")
    _try(repl, main, line=6)
    assert _parses(tmp_path) == 1


def test_a_header_lean_reads_differently_falls_back(fake, monkeypatch):
    repl, main, _, log = fake
    monkeypatch.setenv("FAKE_HEADER_MODULES", "Probe.Basic,Probe.Hidden")
    with pytest.raises(rr.ResidentFallback, match="reads this header's imports differently"):
        _try(repl, main)
    # The verdict is kept for the same header; no candidate ever ran.
    with pytest.raises(rr.ResidentFallback, match="differently"):
        _try(repl, main)
    assert "tactic" not in _requests(log)


@pytest.mark.parametrize("header", ["module\n", "prelude\n", "public import Probe.Basic\n"])
def test_a_module_system_header_falls_back_before_starting_anything(fake, header):
    repl, main, _, log = fake
    main.write_text(header + _FILE, encoding="utf-8")
    with pytest.raises(rr.ResidentFallback, match="header"):
        _try(repl, main, line=6)
    assert _requests(log) == []


def test_a_header_check_that_fails_falls_back_and_is_tried_again(fake, tmp_path):
    repl, main, _, _ = fake
    repl.header_command = [sys.executable, "-c", "import sys; sys.exit(3)"]
    with pytest.raises(rr.ResidentFallback, match="could not check the header"):
        _try(repl, main)
    parser = tmp_path / "fake_parser.py"
    repl.header_command = [sys.executable, str(parser)]
    assert _try(repl, main)["attempts"][0]["outcome"] == "closed"


def test_a_stale_import_is_used_as_built_and_named(fake):
    repl, main, olean, log = fake
    source = main.parent / "Probe" / "Basic.lean"
    later = olean.stat().st_mtime_ns + 10**9
    os.utime(source, ns=(later, later))
    result = _try(repl, main)
    assert result["attempts"][0]["outcome"] == "closed"
    assert "Changed since their last build, so the REPL used that build: Probe.Basic." in result["note"]
    assert "Changed since" in format_tactic_attempts(result)


def test_a_rebuild_below_a_direct_import_starts_a_fresh_process(fake):
    repl, main, olean, log = fake
    project = main.parent
    deep_source = project / "Probe" / "Deep.lean"
    deep_source.write_text("theorem deep : True := trivial\n", encoding="utf-8")
    (project / "Probe" / "Basic.lean").write_text("import Probe.Deep\n", encoding="utf-8")
    deep_olean = olean.parent / "Deep.olean"
    deep_olean.write_bytes(b"olean")
    built = deep_source.stat().st_mtime_ns + 2 * 10**9
    for path in (olean, deep_olean):
        os.utime(path, ns=(built, built))
    _try(repl, main)
    rebuilt = built + 10**9
    os.utime(deep_olean, ns=(rebuilt, rebuilt))
    _try(repl, main)
    assert repl.restarts == 2


def test_a_process_that_cannot_be_stopped_blocks_the_next_start_and_close(fake, monkeypatch):
    repl, main, _, _ = fake
    _try(repl, main)
    real_kill = rr._kill_subprocesses

    def stuck(*args, **kwargs):
        raise RuntimeError("group still live")

    monkeypatch.setattr(rr, "_kill_subprocesses", stuck)
    repl._stop()
    assert repl.stats()["unreaped"] == 1
    with pytest.raises(rr.ResidentFallback, match="has not exited"):
        _try(repl, main)
    with pytest.raises(RuntimeError, match="has not exited"):
        repl.close()
    monkeypatch.setattr(rr, "_kill_subprocesses", real_kill)
    repl.close()
    assert repl.stats()["unreaped"] == 0


def test_stderr_keeps_only_its_tail(fake):
    repl, main, _, _ = fake
    noisy = "import sys; sys.stderr.write('x' * 100000 + 'THE END'); sys.stderr.flush(); sys.exit(1)"
    repl.repl_command = [sys.executable, "-c", noisy]
    with pytest.raises(rr.ResidentReplError) as caught:
        _try(repl, main)
    assert str(caught.value).endswith("THE END")
    assert len(str(caught.value)) < rr.STDERR_TAIL_BYTES + 100


def test_memory_far_past_the_ceiling_stops_a_batch_early(fake, monkeypatch):
    repl, main, _, _ = fake
    monkeypatch.setattr(rr, "RSS_CHECK_SECONDS", 0.0)
    monkeypatch.setattr(repl, "_rss", lambda: int(2 * repl.max_rss_bytes))
    result = _try(repl, main, tactics=("omega", "trivial"))
    # The second candidate needs a fresh process, which the batch then starts.
    assert [attempt["outcome"] for attempt in result["attempts"]] == ["closed", "closed"]
    assert repl.restarts == 2


def test_memory_just_past_the_ceiling_waits_for_the_batch(fake, monkeypatch):
    repl, main, _, _ = fake
    monkeypatch.setattr(rr, "RSS_CHECK_SECONDS", 0.0)
    monkeypatch.setattr(repl, "_rss", lambda: int(1.2 * repl.max_rss_bytes))
    _try(repl, main, tactics=("omega", "trivial"))
    assert repl.restarts == 1
    assert repl.stats()["running"] is False


def test_the_runtime_falls_back_when_the_repl_process_fails(fake):
    repl, main, _, _ = fake
    repl.repl_command = [sys.executable, "-c", "import sys; sys.exit(1)"]
    sessions = []

    def lsp_factory(root):
        sessions.append(_FakeLsp(root))
        return sessions[-1]

    services = LeanRuntimeServices(
        runtime_config(tactic_backend="repl"),
        repl_factory=FakePool,
        lsp_factory=lsp_factory,
        resident_factory=lambda root: repl,
        start_sweepers=False,
    )
    try:
        params = {"project_dir": str(main.parent), "file_path": "Main.lean", "owner": "me", "tactics": ["omega"]}
        out = services.dispatch("lsp.try_tactics", {**params, "line": 5})
        assert "Checked with the language server, not the resident REPL: the REPL exited" in out
        assert sessions[0].calls == [(5, ["omega"])]
    finally:
        services.close()
