# Raise copied code into shared declarations

Workers keep the helpers they add in their own article's file, so a later
worker that needs one copies it. The [subagent debrief](debrief-subagent.md)
asks each worker for the code it copied (`copied_code`), and `autoform
abstraction list` turns the answers into a to-do list of clusters of code that
now exists more than once (see [copied
code](../../../autoform_cli/reference/abstraction.md)). It lives in the debrief
store, `abstractions/TODO.md`, which every worktree shares.

Raise copies only once no worker of the run is running, after the frontier loop
has stopped and before the orchestrator debrief. When the run uses claims, the
refactor subagent runs `work import-impact` for each module whose imports it
changes, then acquires in one command, naming each target once, every edited
article's claim target and each report's `claim_targets`; if refused, it leaves
the cluster open.

1. Run `autoform abstraction list <PROJECT> --json`; without open clusters,
   there is nothing to do.
2. For each open cluster, read the copies and their source. Dismiss with
   `autoform abstraction close <CLUSTER> <PROJECT> --dismissed --note <why>` a
   cluster whose copies are gone or have diverged into different statements.
   Leave open, with that branch as its blocker, a cluster with a copy on a
   handed-off branch the lead has not integrated: that copy is not gone, and
   `close` would cover it. Otherwise record the shared file with `autoform
   abstraction target <CLUSTER> <PROJECT> --file <path>`: an existing project
   module whose imports already provide what the declaration needs and that
   every copy's module can import without a cycle, else a new module beside the
   lowest module that needs it, which the library root imports or the lakefile's
   globs cover.
3. Give each cluster to one refactor subagent, one at a time, in a worktree on
   the shared branch, with the cluster's JSON and target file. It states the
   declaration once in the target file, public, named for what it states and no
   more general than every copy needs; replaces each copy, and the source, with
   a use of it, or makes the source the shared declaration when the source's
   module is the target; and follows [import edits](../SKILL.md#import-edits)
   under those claims. When the cluster's JSON names a `declaration` (a new copy
   reopened a raised cluster), it points the new copies at that declaration
   instead of stating another. It changes proofs only, not an article's
   statement, recorded `lean:` declaration or assertions: a cluster that needs
   that is a revision under the [revision
   contract](../../../autoform_cli/reference/claims-and-revisions.md#revision-contract),
   so it leaves it open and reports it. It finishes with the default `lake
   build`, `#print axioms` on the recorded declarations of every article whose
   Lean it changed, `autoform check`, `autoform audit` and the Agent Review
   [SKILL.md](../SKILL.md) requires of every changed proof, and reports its
   commit, the shared declaration and the files it changed.
4. Integrate the commit as any other result and rerun the default build, then
   record `autoform abstraction close <CLUSTER> <PROJECT> --raised
   <declaration> --file <path>`. The final report names the clusters raised and
   those left open, each with its blocker.
