# Copied code

What `autoform abstraction` reads, groups and records. The [CLI
reference](../README.md#debrief) gives the synopses, and only subagent
debriefs, which need `AUTOFORM_SUBAGENT_DEBRIEF=1`, report copies.

## Copies

The subagent form asks for `copied_code`: the project's own code the agent
copied because it could not use it where it was (a `private` declaration, a
declaration in a module it could not import, or steps inside another proof).
`record` stores those paths relative to the worktree that holds them, so copies
reported from different worktrees match.

## Clusters

`list` reads the copies from the debrief records of succeeded attempts (a failed
attempt's copy never reached the shared branch) and groups them into clusters:
copies of one declaration, and copies of those copies, share a cluster; a copy
of inline code is a cluster of its own until someone copies it in turn. A
cluster's ID is the entry ID (`<attempt prefix>.<index>`) of its oldest copy;
every command also accepts the entry ID of any copy in it. `list` shows open
clusters, or all with `--all`, and marks a copy whose file no longer names it as
no longer present. With `--json` it prints `{"clusters": [{"cluster_id",
"status": "open" | "raised" | "dismissed", "file", "declaration", "note",
"copies": [{"entry_id", "attempt_id", "node_id", "recorded_at", "kind",
"source_file", "source_declaration", "copy_file", "copy_declaration", "what",
"present"}]}]}`.

## Decisions

`target` records the shared file a cluster should be raised into; `close`
records that it was raised into a declaration, or dismissed with a note. Each
decision covers the copies the cluster holds when it is made, so a later copy of
the same code opens the cluster again. Decisions are created exclusively under
`abstractions/` in the debrief store, never overwritten, and `record`, `list`,
`target` and `close` rewrite `abstractions/TODO.md` there, the readable to-do
list. None of them edits the project.
