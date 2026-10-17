---
name: debrief-orchestrator
description: >-
  Once a Setup, Roadmap, or Formalize run is over, record what got in the way:
  friction from the user's local setup, kept apart from friction with AutoformBot
  itself, and draft an AutoformBot issue from the latter for the user to review
  and send. On by default; `AUTOFORM_DEBRIEF=0` turns it off.
---

# Debrief a finished run

The agent that led the run debriefs once, after its final validation and before
its final report. Subagents do not run this skill; fold the AutoformBot friction
they reported to you into your own answer.

There is no `autoform` on `PATH` in a Lean project. Run every `autoform`
command, from `<PROJECT>`, as `uv run --project "<AUTOFORM_PLUGIN_ROOT>"
autoform` followed by its arguments, where `<AUTOFORM_PLUGIN_ROOT>` is the
absolute plugin root: the directory that holds this skill's `skills/` folder,
along with `pyproject.toml` and `autoform_cli/`. Give a subagent that root and
this prefix with its task.

Run `autoform debrief orchestrator form <PROJECT> --skill
setup|roadmap|formalize`, naming the skill that ran (see the
[CLI reference](../../autoform_cli/README.md#commands)). If it says orchestrator
debriefs are disabled, stop: there is nothing to do.

Otherwise answer the form from your own experience of the run and store the
answer with `autoform debrief orchestrator record <PROJECT> --skill <SKILL>`,
passing it with `--answer FILE` or on standard input. If `record` rejects the
answer, correct it and resubmit.

When the answer reports friction with AutoformBot, `record` writes a draft issue
for `facebookresearch/autoform-bot` outside the working tree and prints a link
that opens it pre-filled. The user does not see command output, so copy the
draft's path and the full link verbatim into your final report, and say that
nothing has been sent. Never submit the issue
yourself, with `gh` or otherwise: the user reviews it, edits it, and sends it.
Local paths are replaced by placeholders, but the draft is only as private as
your answer, so leave project-specific content out of it.

Debriefs are on by default. A user who does not want them sets
`AUTOFORM_DEBRIEF=0`.
