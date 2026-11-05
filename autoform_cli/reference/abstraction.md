# Copied code

`autoform abstraction` groups the code workers copied, as subagent debriefs
(`AUTOFORM_SUBAGENT_DEBRIEF=1`) report it in `copied_code`: the project's own
code an agent could not use where it was (a `private` declaration, a declaration
in a module it could not import, or steps inside another proof). `debrief
subagent record` stores those paths relative to their worktree, so they match
across worktrees. None of these commands edits the project:

```bash
autoform abstraction list . --json
autoform abstraction target 3f9c2a1b7d4e.1 . --file Project/Shared/Order.lean
autoform abstraction close 3f9c2a1b7d4e.1 . --raised Project.foo_le --file Project/Shared/Order.lean
autoform abstraction close 3f9c2a1b7d4e.2 . --dismissed --note "the copies diverged"
```

`list` reads the copies that succeeded attempts reported and groups them into
clusters: copies of one declaration, and copies of those copies, share a
cluster; a copy of inline code is a cluster of its own until someone copies it
in turn. A declaration matches by its file and the last component of its name
(`A.foo` and `B.foo` in one file match), and an unknown file or name (empty,
`null` or `none`) matches nothing. A cluster's ID is the entry ID (`<attempt
prefix>.<index>`) of its oldest copy, and every command also accepts the entry
ID of any of its copies. `list` shows open clusters, or all with `--all`, and
marks a copy whose file no longer names it as no longer present. With `--json`
it prints `{"clusters": [{"cluster_id", "status": "open" | "raised" |
"dismissed", "file", "declaration", "note", "copies": [{"entry_id",
"attempt_id", "node_id", "recorded_at", "kind", "source_file",
"source_declaration", "copy_file", "copy_declaration", "what", "present"}]}]}`.

`target` records the shared file a cluster should be raised into; `close`
records that it was raised into a declaration, or dismissed with a note. Each
prints the copies its decision covers, those the cluster holds then; a later
copy opens the cluster again, keeping what it was last raised as, else why it
was dismissed. Decisions are new files under `abstractions/` in the debrief
store, never overwritten. `list`, `target`, `close` and a `record` that reports
copies rewrite the to-do list there, `TODO.md`, and only warn if they cannot.
