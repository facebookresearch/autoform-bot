# Autoform debriefs

What the `autoform debrief` commands ask, store and print. The [CLI
reference](../README.md#debrief) gives the synopses.

## Orchestrator debriefs

Orchestrator debriefs are on by default: `form` prints the form unless
`AUTOFORM_DEBRIEF` is `0`, `false`, `no`, or `off`, or the platform cannot store
the answer, and otherwise says the step is disabled. The form asks separately
for friction from the user's local setup (`local_setup_friction`) and with
AutoformBot itself (`autoform_friction`). `record` reads one JSON object
answering the form from `--answer` or standard input, refuses anything else, and
stores it under `runs/` with the installed AutoformBot version; a directory
without a roadmap, as during Setup, is accepted. When `autoform_friction` is not
empty, `record` writes a draft issue for `facebookresearch/autoform-bot`
under `issues/` and prints its path. The draft holds only that friction and the
environment, with the project path replaced by `$PROJECT` and home directories
by `~`, and ends, below a `---` footer, with a link to GitHub's new-issue form
pre-filled with the draft as recorded (only its title when the draft is too
long, as the footer and `record` say), so edit in the form. Nothing is sent.
With `--json`, `record` prints `{"run_id": ..., "path": ..., "issue": null |
{"path": ..., "url": ..., "url_has_body": true | false}}`.

## Subagent debriefs

Subagent debriefs are opt-in: `form` prints the form only when
`AUTOFORM_SUBAGENT_DEBRIEF=1` on a platform that can store the answer, with the
outcome filled in from the runtime: the phase succeeded when the article's
statement (for `statement`) or proof (for `proof`) is now recorded as
formalized. `record` takes one JSON object as above, re-reads the outcome from
the runtime, stores a new record, and never edits the project; it requires
`--worker-id` or `AUTOFORM_WORKER_ID`. With `--json` it prints `{"attempt_id":
..., "outcome": "succeeded" | "not-succeeded", "path": ...}`, `path` being the
stored record. `render` rebuilds `views/` from the validated records, escaping
agent text and deleting reports whose record is gone.

## The store

Both kinds share one store, `autoform/debriefs/` in the repository's Git common
directory, which every worktree shares and Git does not track, or outside Git
`$XDG_STATE_HOME/autoform/debriefs/<project>-<hash>/`. Set
`AUTOFORM_DEBRIEF_DIR` to an absolute path outside the Git working tree to
choose another. A record is never overwritten; platforms that cannot retain
directory descriptors, such as Windows, refuse to read or write the store.
