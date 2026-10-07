"""Opt-in integration tests against the pinned upstream Lean REPL."""

from __future__ import annotations

import os
import re
from contextlib import suppress
from pathlib import Path

import pytest

from servers.lean_client import LeanRuntimeClient, LeanRuntimeUnavailable
from servers.repl.core import LeanRepl, LeanReplConfig


REPL_FIXTURE = Path(__file__).parent / "fixtures" / "repl-smoke"
pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOFORM_RUN_REAL_REPL_TESTS") != "1",
    reason="set AUTOFORM_RUN_REAL_REPL_TESTS=1 to run pinned REPL integration",
)


def test_disposable_call_matches_the_pinned_repl_protocol():
    repl = LeanRepl(LeanReplConfig(cwd=str(REPL_FIXTURE), warmup_imports=frozenset(), validate_imports=False))

    response = repl.run_disposable("theorem autoform_repl_probe : True := by sorry", timeout=180)

    assert response.get("sorries")
    assert "env" not in response
    assert all("proofState" not in sorry for sorry in response["sorries"])
    assert repl.is_clean()


def test_fixture_exposes_escaped_module_traversal_without_policy_validation():
    repl = LeanRepl(
        LeanReplConfig(
            cwd=str(REPL_FIXTURE),
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )

    response = repl.run_disposable(
        "import Mathlib.«..».Secret\n#check escapedTraversalMarker",
        timeout=180,
    )

    assert "repl_error" not in response
    assert not any(
        message["severity"] == "error" for message in response.get("messages", [])
    )
    assert repl.is_clean()


@pytest.mark.parametrize(
    ("warmup", "code", "expected_error"),
    [
        ((), "/- note -/\nimport Init.Data\n#check Nat", "Disallowed imports: Init"),
        ((), "module\npublic import Init.Data\n", "Disallowed imports: Init"),
        (("REPL",), "/- note -/ import Init.Data\n#check Nat", "Disallowed imports: Init"),
        ((), "import NotAllowlisted.Mod\n", "Disallowed imports: NotAllowlisted"),
        ((), "import «REPL\n", "Rejected Lean header"),
        (
            (),
            "/- a --/\nimport REPL.Frontend\n-- -/\n#check Nat",
            "cannot validate safely",
        ),
        (
            (),
            "prelude -- ordinary comment\n"
            "import Init /- a --/\n"
            "import Lean\n"
            "-- -/\n"
            "#check Lean.Name",
            "cannot validate safely",
        ),
        (
            (),
            "import Mathlib.«..».Secret\n#check escapedTraversalMarker",
            "plain, nonempty module components",
        ),
        ((), "import REPL.Frontend\n#check Nat", None),
        ((), '#eval "--/"', None),
        ((), "--/ harmless line comment\n#check Nat", None),
        ((), "#check Nat\n/- body --/", None),
        (("Mathlib",), "#check autoformWarmupMarker", None),
        (("Mathlib",), "module\npublic import REPL.Frontend\n", None),
        (("Mathlib",), "prelude\nimport REPL.Frontend\n#check Nat", None),
    ],
)
def test_disposable_imports_are_checked_by_lean(warmup, code, expected_error):
    repl = LeanRepl(
        LeanReplConfig(
            cwd=str(REPL_FIXTURE),
            allowed_imports=frozenset({"Mathlib", "REPL"}),
            warmup_imports=frozenset(warmup),
        )
    )

    response = repl.run_disposable(code, timeout=180)

    if expected_error is None:
        assert "repl_error" not in response
        assert not any(message["severity"] == "error" for message in response.get("messages", []))
    else:
        assert expected_error in response["repl_error"]
    assert repl.is_clean()


def test_runtime_calls_use_distinct_disposable_processes(runtime_dir, monkeypatch):
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_REPL_WORKERS_PER_PROJECT", "1")
    client = LeanRuntimeClient(socket_path=runtime_dir / "real-repl.sock", response_timeout=300, startup_timeout=30)
    params = {
        "project_dir": str(REPL_FIXTURE),
        "code": "#eval IO.Process.getPID",
        "timeout": 180,
    }
    try:
        responses = [client.request("repl.run", params) for _ in range(2)]
    finally:
        with suppress(LeanRuntimeUnavailable):
            client.stop()

    process_ids = []
    for response in responses:
        match = re.search(r"info: (\d+)", response)
        assert match is not None, response
        process_ids.append(int(match.group(1)))
    assert process_ids[0] != process_ids[1]


def test_runtime_warmup_prefix_preserves_source_line_numbers(runtime_dir, monkeypatch):
    monkeypatch.setenv("AUTOFORM_REPL_TOTAL_WORKERS", "1")
    monkeypatch.setenv("AUTOFORM_REPL_WORKERS_PER_PROJECT", "1")
    client = LeanRuntimeClient(
        socket_path=runtime_dir / "real-repl-lines.sock",
        response_timeout=300,
        startup_timeout=30,
    )
    try:
        response = client.request(
            "repl.run",
            {
                "project_dir": str(REPL_FIXTURE),
                "code": "#check autoformMissingIdentifier",
                "timeout": 180,
            },
        )
    finally:
        with suppress(LeanRuntimeUnavailable):
            client.stop()

    assert re.search(r"\b1:\d+: error:", response), response
