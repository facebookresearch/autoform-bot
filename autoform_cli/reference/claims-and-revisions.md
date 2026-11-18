# Autoform claims and revisions

## Claim contract

Claims use canonical `autoform-claim/v1` JSON in orphan commit messages and
exact observed object IDs as update preconditions. Absent and verifiably expired
leases may be acquired; live peer leases are refused. Malformed or unreadable
refs are unverifiable and may not be acquired or released.

A claim key is a slug and digest of any string, not a validated node id, so a
shared resource, or a Lean helper no article owns (under the
`lean/<slug>-<digest>` key both impact commands report), is locked like a node.
Parallel agents get one Git worktree each; builds take no claim.

`acquire` and `release` change all their targets in one atomic push,
or none; malformed targets fail before any push, and a board that cannot push
atomically is refused.
A refusal exits 1 with one line such as `error: could not acquire af_y, af_z;
no claim was acquired: held by another worker: af_y`, naming the blocking
targets when known; a duplicate target exits 2. Workers that need several
claims follow a no-hold-and-wait rule, which the CLI does not enforce: acquire
the whole set in one command and, when it is refused, release everything already
held and retry with the whole set.

`claim list` copies the board and prints each lease as its ref named it when the
board answered. It is not a consistent snapshot: a peer's multi-claim push made
meanwhile can show some of its claims changed and others not. A listing is a
report, not a lock: every acquire and release checks ownership again.

`claim list TARGET...` lists only those targets' claims, in the same JSON,
leaving out a target without a claim; a duplicate target exits 2. It fetches
only those refs but still downloads the board's whole list of refs. `--timeout
SECONDS` (default 120) bounds the listing's Git commands, not removing its own
refs afterwards, and a step still running after five seconds is named on stderr
as `claim list: <step>...`. `claim list` runs Git without a terminal on Linux
and macOS, and stops a command that times out or is interrupted with everything
it started. A failed `claim list` exits 1 with nothing on stdout and, after any
progress lines, an error on stderr that may span lines and starts `error:
timeout:` when time ran out, naming the step; `error: contention:` when the
board refused three fetches because claims kept moving; or `error: transport:`
for any other failure to reach or read the board. Other errors, such as a
missing `origin` or `--worker-id`, have no label. Claim commands run Git with
`GIT_TERMINAL_PROMPT=0` and no input, but `ssh`, a credential helper or an
askpass program can still prompt, and a command waits for it until its time
limit. They ignore `GIT_DIR`, `GIT_WORK_TREE` and the other variables that point
Git at another repository, so they work inside a Git hook.

A Git process that stops while updating a claim ref leaves a lock file that
blocks updates: `refs/autoform-claims/<key>.lock` blocks that claim,
`packed-refs.lock` every release, and on a reftable board
`reftable/tables.list.lock` every claim update. On a board given as a local
path, or as a `file://` URL except on Windows, an acquire or release that such a
file still blocks two seconds later exits 1 naming the file and its modification
time (one gone within two seconds counts as a lost race), and `claim list` warns
on stderr about each lock file not modified within two seconds of the current
time. Over any other transport a lock file shows up as a lost race or as Git's
own error. A terminal's interrupt reaches Git, which then removes its lock, in
every claim command but `claim list`. Autoform never deletes a lock file: once
no Git process is using the board, delete it and retry.

`acquire --note TEXT` stores free text in each lease, and `claim list` shows it
as `note`; the key is absent, not empty, after an acquire without `--note`. Each
lease also records its `target`, the string its key was hashed from, so `claim
release --worker-id OWNER TARGET` frees a stopped worker's claim by what `claim
list` shows. A new claim lasts until it is released. Delete a claim ref by hand
only when the current `claim list TARGET` shows `_malformed: true`, pushing a
deletion of `refs/autoform-claims/<_key>` leased on its `_oid`.

Claims are temporary operational state, never article frontmatter.

## Revision contract

Revising a declaration X of article R that other articles' Lean uses touches
work R's claim does not cover. This contract makes that work claimable and
keeps the default build passing. Roadmap records a requested revision in the
blueprint (step 6); Formalize carries out the Lean side (steps 1 to 5).

1. On a fresh build, run `autoform work impact R . --lean-root .`, with
   `--declaration` when only some of R's declarations, or a helper, change.
2. Choose the route:
   - **Contained** (`contained: true`): revise X in place, under R's claim when
     using claims. `contained` ignores R's own declarations, so also re-check
     R's other declarations that use X.
   - **Expand, migrate, contract**, the default whenever anything uses X: add
     X' with the revised statement, leave X unchanged and mark it
     `@[deprecated X' (since := "YYYY-MM-DD")]`, and point R's `lean:` at X'.
     Under the open policy, while X's proof is still `sorry`, R's `lean:`
     names X beside X', so the audit keeps accepting that `sorry`, and R
     records `proof` only after step 4 deletes X. Statement-impacted articles
     are retracted with step 6's call, returning to the frontier as revisions
     (under the open policy a statement-impacted theorem stays an [open
     statement](open-statements.md#open-statements) meanwhile). When X's proof
     is sorry-free, proof-impacted articles keep everything, since their proofs
     still use the valid old X; migrating them to X' is later work. While it is
     still `sorry`, proof-impacted theorems lose `proof: formalized` but keep
     `statement` and `lean:`, returning to the frontier as proof phases that
     migrate to X', and a proof-impacted definition, which counts as proved
     whatever its `proof:` says, is migrated to X' in the same commit or, when
     that is not possible, retracted like a statement-impacted article. The
     claim set is R, the statement-impacted articles, the unused statement
     dependencies, and, in that case, the proof-impacted ones.
   - **In place**, only when X and X' cannot coexist, for example an instance
     or a structure change: the claim set is every `claim_targets` entry, and
     every impacted declaration is repaired in one commit whose default build
     passes. A change `work impact` cannot see, to an instance's priority or
     scope, an attribute, or notation, also takes this route whatever
     `contained` says; its `claim_targets` are incomplete, so add every stated
     article whose Lean imports the changed module as statement-impacted. A
     statement-impacted article keeps `statement` only after an Agent Review
     of its source faithfulness under X's new meaning; otherwise it is
     retracted (`statement: retracted`, no `proof`, `lean:` kept). A repaired
     dependent proof keeps `proof: formalized` only after an Agent Review of
     the repair; otherwise it loses `proof`. A theorem's proof that cannot be
     repaired becomes exactly `sorry` under the open policy; otherwise delete
     the declaration and remove its article's `lean:`, `statement`, and
     `proof`, which works only when nothing else uses it. When neither
     applies, the revision is blocked: release any claims and report it.
     Record what happened in the revision commit and report. Use an
     implementation note only until [its deletion
     rule](blueprint-format.md) applies.

   An unused statement dependency rules out the contained route and is
   re-reviewed under the revised meaning, X' on the expand route and X's new
   meaning in place: it keeps `statement` only after an Agent Review of its
   source faithfulness; otherwise it is retracted the same way.
3. When using claims, claim the route's claim set with one
   `autoform claim acquire`; when it is refused, release everything and report
   the held claim as the blocker. After acquiring, re-run `work impact`; if the
   set grew, release and start over with the larger set. Without claims, a
   Formalize subagent has its lead confirm the set instead. After rebasing onto
   the current shared branch and rebuilding, re-run `work impact` once more; if
   the set grew, acquire the whole larger set in one command under the
   no-hold-and-wait rule of the [claim contract](#claim-contract) and repair the
   new targets before landing. Under the open policy, reproduce the CI audit as
   [open statements](open-statements.md#open-statements) shows before landing.
   Land one commit, then release any claims.
4. Contract: delete a deprecated X once it appears in `deprecated_unused` of
   `work impact R . --lean-root .`, meaning no declaration uses it and no
   other article's `lean:` names it, and drop it from R's `lean:` in the same
   commit. `contained` is not enough: it ignores an X' built from X.
5. `autoform audit --lean-root` reports `lean-target-deprecated` when a
   declaration article's `lean:` names a declaration with `deprecated` in its
   own `@[...]` list; point it at the replacement. The check is lexical, so it
   misses a later `attribute [deprecated] X`, which `work impact` sees. Under
   the open policy the finding is expected for the X that step 2 keeps in R's
   `lean:` until step 4, so `autoform audit --lean-root` and `autoform doctor
   --lean-root` fail until then, while CI, which runs neither, passes.
6. When Roadmap revises an article, its statement text or only its Lean, it
   retracts only that article and the dependents whose text the revision
   rewrites (`autoform links set blueprint <ARTICLE_PATH> statement=retracted
   --unset proof`, keeping `lean` for `work impact`, where `<ARTICLE_PATH>` is
   each one's path from the project root, a folder article's `README.md`), and
   releases any claims. A `mathlib: true` article's call adds `--unset mathlib
   --unset mathlib_declaration --unset mathlib_file`, or, without `lean:`, is
   only those. [Roadmap](../../skills/roadmap/SKILL.md#claims-and-revisions)
   adds when to claim, rebase and report.

With the review marker, a commit that drops a declaration from an article's
`lean:` also deletes that declaration's read-back cards under
`blueprint/readbacks/<article_id>/`, which `review check` otherwise reports as
orphaned, and one that removes `lean:` also removes `review_approved`.
