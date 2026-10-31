# Claims in a Formalize run

Follow these rules when a [Formalize](../SKILL.md) run uses claims. They replace
asking the lead before editing beyond what a worker holds and before an import
edit lands. The [claim contract](../../../autoform_cli/reference/claims-and-revisions.md#claim-contract)
defines the board.

## Worker IDs and the board

Give every concurrent agent and subagent its own worker ID, such as its host and
worktree name, and pass it as `--worker-id` on every claim command. Never reuse
the lead's, because a board accepts a second acquire from the same owner, and do
not rely on exporting `AUTOFORM_WORKER_ID` once: a later tool call can start
from a profile that sets a shared value. Run claim commands from the project
worktree so every worker uses its `origin` board, or pass the same `--repo`
everywhere when it has none. Claims write refs to that board's remote, which is
outward-facing: make sure the request covers it before the first claim.
A session leads from [its own clone](../../../README.md#run-several-sessions):
if `git worktree list` shows the shared branch elsewhere, ask the user first.

## Acquire and renew

Before editing, acquire the exact `claim_target` from `work context`, each time
with `--note` naming the worktree's branch. A failed acquire or renewal means
ownership is unproven: stop writing. Confirm a claim with `renew`, not acquire:
every five minutes (leases expire after 25) and just before integrating.

## Import edits

Compare the import-impact report's claim keys with `autoform claim list`, as the
[CLI reference](../../../autoform_cli/README.md#work-import-impact) describes: another
owner's live affected claim is a conflict, malformed evidence is unverifiable,
and an expired valid claim is not active. Before the edit, atomically acquire
the required article and module `claim_targets`; an already-held same-worker
article claim is upgraded, not released first. Recheck affected keys after
acquisition and before integration; these checks are best-effort coordination,
not a lock on downstream articles. Before repairing any article that actually
needs changes, listed as affected or not, atomically acquire the full held set
plus its targets. On a refused import-claim acquisition or a recheck conflict,
release the complete currently held import set before waiting or reporting,
unless handing off an existing verified candidate as described below. Renew,
hand off, and release the full import claim set together.

## Generated import roots

Unlike other import edits, a root that the repository's deterministic generator
writes from the tracked modules, such as a `Lib.lean` that imports every module
of a folder, is not part of a worker's long-held claim set; a root written or
edited by hand, or one with no such generator, keeps the import-edit rules
above. A worker may regenerate it in its worktree to build and check its result,
without claiming it, and may commit it; on a conflict in it during a rebase or
merge, regenerate it from the merged tree. Before landing a commit that changes
the root, the integrator acquires the root's module `claim_target` together
with the complete set it holds, in one command under that set's worker ID, then
regenerates it from the integrated tree. If another owner holds the root, follow
the refusal rule under Import edits:
release the complete held set before waiting, or keep it and hand off the
verified candidate. Then re-run `autoform work import-impact` and the checks on
the result, and release the root's claim with the rest of the set once the
result is visible, as the next section requires.

## Integration and handoff

Renew the complete claim set before integrating, and if a revision's claim set
grew, acquire the whole larger set in one command under the [no-hold-and-wait
rule](../../../autoform_cli/reference/claims-and-revisions.md#claim-contract) before repairing the new
targets. Keep the complete acquired set until every checkout on the claim board
can see the verified commit: on the shared branch and, for an `origin` board,
pushed, since other clones read their frontier from the remote. No message
grants ownership or completes a leaf. For a handoff, keep the set and report it
with the worker ID instead of making the leaf look free; when the lead hands it
to another session and the request covers pushing, push the candidate to
`origin` on a branch ending with the leaf's `article_id`. The integrator renews,
integrates, and releases a handed-off set with `--worker-id` set to the reported
ID; without a report, it waits for the lease to lapse. After a lapse, it reads
any note left, looks for that branch with `git ls-remote --heads origin` to
continue it, acquires the complete set under its own ID and, before integrating,
repeats Formalize's pre-edit reload of `work context`. Release the complete set
once the result is visible that way, or when abandoning the leaf without a
candidate. For ordinary work that set is just the article; for an import edit it
also includes the module and any validated expansion.
