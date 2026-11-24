"""Unattended contribution loop: ``autoform work run [--loop]``.

A worker repeats rounds. Each round does one unit of work, the first that
applies: maintain one of the loop's own open pull requests (answer a person,
merge the base branch into a conflicting branch, address the review bot's
requested changes, repair a failed check), or author a new ready task. An
author attempt claims the task on the claim board, runs a coding agent without
GitHub credentials, verifies the result itself, and only then pushes a branch
and opens a ready pull request, which is the task's public claim. The loop
never merges, approves, or touches pull requests it did not open.

The modules: :mod:`.profile` (what the loop knows about a project),
:mod:`.state` (the shared state directory), :mod:`.gitops` and
:mod:`.github` (the only writers to remotes), :mod:`.tasks` (task lists and
picking), :mod:`.agents` (Claude Code and Codex), :mod:`.verify` (the
harness's own checks), :mod:`.prstate` (reading a pull request's state),
:mod:`.worker` (rounds) and :mod:`.cli` (the command line and the
supervisor of several workers). ``autoform_cli/reference/work-loop.md`` is the user contract.
"""

from __future__ import annotations
