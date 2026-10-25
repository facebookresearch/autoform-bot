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

There is no `autoform` on `PATH` in a Lean project. Run it from `<PROJECT>` as
`uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform`, where the root is the
absolute plugin directory holding `skills/`, `pyproject.toml`, and
`autoform_cli/`; give a subagent that prefix, with the root filled in, and its
task.

Run `autoform debrief orchestrator form <PROJECT> --skill
setup|roadmap|formalize`, naming the skill that ran (see the
[CLI reference](../../autoform_cli/README.md#debrief)). If it says orchestrator
debriefs are disabled, stop: there is nothing to do.

Otherwise answer the form from your own experience of the run and store the
answer with `autoform debrief orchestrator record <PROJECT> --skill <SKILL>`,
from `--answer FILE` or standard input. If `record` rejects it, correct it and
resubmit.

When the answer reports friction with AutoformBot, `record` writes a draft issue
for `facebookresearch/autoform-bot` outside the working tree and prints its
absolute path. The user does not see command output, so end your final report
with the draft as a Markdown link to the whole path, exactly as printed and in
angle brackets; the draft usually lives under `.git`, which editors hide:
`[Draft AutoformBot issue](</home/me/proj/.git/autoform/debriefs/issues/RUN_ID.md>)`.
Say that nothing has been sent and that the link at the bottom of the draft
opens the issue form pre-filled with the recorded draft, so edits belong in the
form; if `record` says the draft is too long for a link, say instead that the
link fills in only the title. Do not paste that link itself. Never submit the
issue yourself, with `gh` or otherwise: the user reviews it, edits it, and sends
it. The draft is only as private as your answer, so leave project-specific
content out of it.
