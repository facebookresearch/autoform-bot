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

## Acquire

Before editing, acquire the exact `claim_target` from `work context`, each time
with `--note` naming the worktree's branch. A failed acquire means ownership is
unproven: stop writing, and release those of its targets that `claim list` shows
your ID holding, since a lost reply can hide a push that landed. A claim lasts
seven days.

## Import edits

Compare the import-impact report's claim keys with `autoform claim list`, as the
[work reference](../../../autoform_cli/reference/work.md#work-import-impact) describes: another
owner's live affected claim is a conflict, malformed evidence is unverifiable,
and an expired valid claim is not active. Before the edit, atomically acquire
the report's `claim_targets`; an already-held same-worker article claim is
upgraded, not released first. Recheck affected keys after acquisition and before
integration; a recheck whose `claim list` fails counts as a conflict. Before
repairing any article, listed as affected or not, atomically acquire the full
held set plus its targets. On a refused import-claim acquisition or a recheck
conflict, release the complete held import set before waiting or reporting.

## Generated import roots

This applies with or without claims. A root is generated only when a committed
`.gitattributes` line such as `/Lib/Topology.lean linguist-generated` marks it
and the repository's CI regenerates it and fails on any difference. The user
marks roots and adds or confirms that check; workers never edit attribute files.
`work import-impact` then reports `module_generated` and leaves
`module_claim_target` out of `claim_targets`; without that check, add it back,
claimed or sent to the lead, as for any import edit. No one claims a generated
root; another owner's live claim on it is neither a conflict nor yours to
release. Never edit it by hand: regenerate it with the command that check runs,
from the tree you build and again from the merged tree after every rebase or
merge; rerunning it must leave no change. Before landing, re-run
`work import-impact` and the repository's checks on the result; an affected
target another owner holds is still a conflict. Any other root, whatever its
header says, keeps the import-edit rules.

## Integration and recovery

Just before integrating, re-run `claim acquire` on the complete set; a refusal
means stop. Keep the complete acquired set until every checkout on the claim
board can see the verified commit: on the shared branch and, for an `origin`
board, pushed, since other clones read their frontier from the remote. No
message grants ownership or completes a leaf. Before reporting, the lead runs
`claim list` and releases every claim its own and its workers' IDs still hold,
by the `target` each lease shows, including a set kept for a candidate it hands
off. To take over a stopped worker's set, it releases under that owner's ID,
acquires under its own, stops if refused, and repeats Formalize's pre-edit
reload of `work context`; another session's claim it takes only with the user's
word. Release the complete set once the result is visible that way, or when
abandoning the leaf without a candidate. For ordinary work that set is just the
article; for an import edit it is the report's `claim_targets` and any validated
expansion.
