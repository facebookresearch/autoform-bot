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

## Acquire and renew

Acquire the exact `claim_target` returned by `work context` before editing. A
failed acquire or renewal means ownership is unproven: stop writing. Confirm a
claim with `autoform claim renew`, not acquire: about every five minutes, since
leases expire after 25 minutes, and immediately before integrating.

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

## Integration and handoff

Renew the complete claim set before integrating, and if a revision's claim set
grew, acquire the whole larger set in one command under the [no-hold-and-wait
rule](../../../autoform_cli/reference/claims-and-revisions.md#claim-contract) before repairing the new
targets. Keep the complete acquired set until every checkout on the claim board
can see the verified commit: on the shared branch and, for an `origin` board,
pushed, since other clones read their frontier from the remote. For a handoff,
keep the set and report it with the worker ID instead of making the leaf look
free. The integrator renews, integrates, and releases a handed-off set with
`--worker-id` set to the reported ID; if the lease has lapsed, it acquires the
complete set under its own ID and, before integrating, repeats Formalize's
pre-edit reload of `work context`. Release the complete set once the result is
visible that way, or when abandoning the leaf without a candidate. For ordinary
work that set is just the article; for an import edit it also includes the
module and any validated expansion.
