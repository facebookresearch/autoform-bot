# Claims in a Formalize run

Follow these rules when a [Formalize](../SKILL.md) run uses claims. They replace
asking the lead before editing beyond what a worker holds and before an import
edit lands. The [claim contract](../../../autoform_cli/reference/claims-and-revisions.md#claim-contract)
defines the board.

## Worker IDs and the board

Give every concurrent agent and subagent its own worker ID, such as its host and
worktree name, and pass it as `--worker-id` on every claim command. Never reuse
the lead's, because a board accepts a second acquire from the same owner, and do
not rely on exporting `AUTOFORM_WORKER_ID` once: a later call can start from a
profile that sets a shared value. Run claim commands from the project worktree
so every worker uses its `origin` board, or pass one `--repo` everywhere when it
has none. Claims write refs to that board's remote, which is outward-facing:
make sure the request covers it before the first claim. A session leads from
[its own clone](../../../README.md#run-several-sessions): before dispatching, it
asks the user if `git worktree list` shows the shared branch elsewhere.

## Acquire and renew

Before editing, acquire the exact `claim_target` from `work context`, each time
with `--note` naming the worktree's branch. For a lapsed lease, first continue
any `handoff/<article_id>` branch on `origin`. A failed acquire or renewal means
ownership is unproven: stop writing. Confirm a claim with `renew`, not acquire:
every five minutes (leases expire after 25) and just before integrating.

## Import edits

Compare the import-impact report's claim keys with `autoform claim list`, as the
[CLI reference](../../../autoform_cli/README.md#work-import-impact) describes: another
owner's live affected claim is a conflict, malformed evidence is unverifiable,
and an expired valid claim is not active. Before the edit, atomically acquire
the report's `claim_targets`; an already-held same-worker
article claim is upgraded, not released first. Recheck affected keys after
acquisition and before integration; these checks are best-effort coordination,
not a lock on downstream articles. Before repairing any article that actually
needs changes, listed as affected or not, atomically acquire the full held set
plus its targets. On a refused import-claim acquisition or a recheck conflict,
release the complete currently held import set before waiting or reporting,
unless handing off an existing verified candidate as described below. Renew,
hand off, and release the full import claim set together.

## Generated import roots

This applies with or without claims. A root is generated only when a committed
`.gitattributes` line such as `/Lib/Topology.lean linguist-generated` marks it
and the repository's CI regenerates it and fails on any difference. The user
marks roots and adds or confirms that check; workers never edit attribute files.
`work import-impact` reads the mark from the index or the clone's
`.git/info/attributes`, never the user's or the system's attribute files,
reports it as `module_generated`, and leaves `module_claim_target` out of
`claim_targets`; without that check, add it back, claimed or sent to the lead,
as for any import edit. No one claims a generated root; another owner's live
claim on it is neither a conflict nor yours to release. Never edit it by hand:
regenerate it with the command that check runs, from the tree you build and
again from the merged tree after every rebase or merge; rerunning it must leave
no change. Before landing, re-run `work import-impact` and the repository's
checks on the result; an affected target another owner holds is still a
conflict. Any other root, whatever its header says, keeps the import-edit rules.

## Integration and handoff

Renew the complete claim set before integrating, and if a revision's claim set
grew, acquire the whole larger set in one command under the [no-hold-and-wait
rule](../../../autoform_cli/reference/claims-and-revisions.md#claim-contract)
before repairing the new targets. Keep the complete acquired set until every
checkout on the claim board can see the verified commit: on the shared branch
and, for an `origin` board, pushed, since other clones read their frontier from
the remote. No message grants ownership or completes a leaf. For a handoff, keep
the set and report it with the worker ID instead of making the leaf look free;
when the lead hands it to another session and the request covers pushing, push
the candidate to `origin` as `handoff/<article_id>`. The integrator renews,
integrates, and releases a handed-off set with `--worker-id` set to the reported
ID; without a report, it waits for the lease to lapse, acquires the complete set
under its own ID and, before integrating, repeats Formalize's pre-edit reload of
`work context`. Release the complete set once the result is visible that way, or
when abandoning the leaf without a candidate; in the first case, also delete a
pushed handoff branch. For ordinary work that set is just the article; for an
import edit it is the report's `claim_targets` and any validated expansion.
