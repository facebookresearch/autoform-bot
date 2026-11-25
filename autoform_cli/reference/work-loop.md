# The work loop

`autoform work run` contributes to a Lean project unattended: it claims a ready
task, has a coding agent formalize it, checks the result itself, opens a pull
request, and follows the pull request's checks and reviews until a maintainer
could merge it; `work --loop` is short for `work run --loop`. The [CLI
reference](../README.md#work-run) gives the synopsis.

## Requirements and authority

The loop runs only on Linux and macOS. Set Git's `user.name` and `user.email`.
Sign in to `gh` with your PR account and to `claude` or `codex`.
In your Lean project clone (`origin` on GitHub, or pass `--repo OWNER/NAME`),
run `lake build` (`lake exe cache get` first for Mathlib). Then follow the
[work loop launch instructions](../../README.md#run-agents-in-parallel).

Running it is your authority to push claims, branches and pull requests as
your `gh` account, and to spend on the agents. It pushes only branches under
the profile's prefix (`autoform-work/` by default) of your fork, which it
creates when you have none, or of `--fork OWNER/NAME`, with no tags; it
never pushes the base branch or the push repository's default branch, and a
repair only adds commits. It never merges, approves, enqueues, labels other
pull requests, or comments anywhere but on its own pull requests.
`--dry-run` writes nothing to GitHub or the claim board: it leaves the loop's
pull requests alone and stops each attempt before its first push, keeping the
commit in the worker's worktree.

## Rounds

Without `--loop` a worker does one round; with it, rounds repeat until a stop
(below). A round does one unit of work, the first that applies:

1. Maintain one of the loop's open pull requests, oldest first, one worker per
   pull request: answer a person's comment; merge the base branch into a
   conflicting branch; address the review bot's requested changes on the head;
   repair a failed check.
2. Otherwise, below `--max-open-prs` (default 5), author a new task.

A pull request is the loop's when you opened it against the base branch, its
body holds the loop's hidden `<!-- autoform-work {...} -->` marker, and its
branch is under the prefix on the repository the worker pushes to; each is
checked again on a fresh read before the worker acts. A GitHub error ends the
round, so an outage never reads as having nothing to do.

## An author attempt

1. **Pick.** The task list is the profile's task command, or
   `autoform work list --json` of the checkout. A task is free when no open
   pull request names its article id (a project's picker can be stricter), no
   claim holds it, and it did not fail on this machine in the last 7 days.
   Among the free tasks of the allowed phases, the worker tries them in a
   random order, so that independent operators rarely meet. With `--triage
   SPEC`, that agent first rates a task's feasibility from its statement, 1 to
   5, and only tasks rated at least `--min-score` (default 4) are attempted;
   every worker of the state directory reuses the scores.
2. **Claim.** One `autoform claim acquire` of the task's `claim_target`, on the
   claim board: `--claims-repo`, else the profile's `claims_repo`, else your
   fork. A refused claim moves on to the next task; a board that cannot be
   read ends the round. The board can be public, so a lease names the worker
   (below), never the machine; its note names the task and, once opened, the
   pull request.
3. **Prepare.** The worker's worktree, `workers/<id>/worktree` in the state
   directory, is reset to the base branch's tip. Its `.lake/packages` and its
   first `.lake/build` are copies of the checkout's (copy-on-write where the
   filesystem allows), never links, so its Lake cannot change the checkout's.
   For a proof task, the harness locates each target declaration, builds its
   module and records its statement facts.
4. **Agent.** The agent gets the [prompts](../work_loop/prompts/), the task
   item, the path of the Formalize skill's one-article procedure, and the
   profile's instructions, with `--claude-arg` or `--codex-arg` added to its
   command line. It runs without credentials (no `GH_TOKEN`, an empty
   `GH_CONFIG_DIR`, no SSH agent or SSH transport, Git reading neither your nor
   the system's configuration, and every credential setting of the checkout and
   of its worktree, URL-scoped ones included, emptied), as do the checks run on its change, and
   is told never to commit or push. It writes a
   report: `done` with a title, a pull-request body and a commit message, or
   `blocked` with its evidence, as for a false or vacuous statement.
5. **Verify.** The harness then checks, against the merge base: the change is
   not empty; added Lean lines, outside comments and literals, hold no
   `sorry`, `admit`, `axiom` declaration, `native_decide`, `implemented_by`,
   `unsafe` or `debug.skipKernelTC`; nothing imports a deleted module; every
   changed module builds; for a proof task, each target declaration is now a
   theorem whose elaborated statement and universe parameters equal the base's,
   whose statement uses the same project definitions with the same types and
   values, and whose axioms are only `propext`, `Classical.choice` and
   `Quot.sound` (private names compare by their user names, so moving a
   private definition or the placeholder is no change); and the profile's
   commands pass, leaving the tree as it was verified. A failure goes back to
   the agent twice at most.
6. **Publish.** The harness commits as you (with a `Co-authored-by` line naming
   the agent, unless `--no-co-author`), refuses a title or body that would take
   another task (judged by the base commit's `taken_module` and its directory,
   written from the commit's tree; a reword runs outside the worktree), commits
   exactly the tree it verified, runs the profile's publication check, reads the task list
   again and gives up if another pull request took the task, then pushes the
   branch create-only and opens a ready pull request. Its body is the agent's,
   then `Tasks: <article id>`, the profile's lines, `Autoform project:
   <owner>/<repo>/<blueprint>`, a line saying the loop opened it, and the marker.
   A branch pushed without its pull request is opened ten minutes later; until
   then it keeps its claim, its task is not attempted again, and the loop does
   not exit; only the operator and fork that pushed it reconcile it. GitHub writes
   are never retried blindly: a reply or notice whose response was lost is
   found by its marker rather than posted twice.

An attempt that ends without a pushed branch releases its claim. A release
that fails is retried every round until the claim is gone.

## Following a pull request

Each round reads the loop's pull requests from GitHub, which is the source of
truth. The review bot's verdict is its latest approving or change-requesting
review of the head carrying one of the profile's markers. Its comments are never
taken for a person's. Cancelled checks, checks waiting for a maintainer (a
first-time contributor's workflow runs), checks the profile names as the review
bot's, and checks that fail on the base branch too are never repaired. A repair is
verified like an attempt, committed on the branch and pushed leased on the head
it started from, never force-pushed. A merge of the base branch commits a merge.
A finding the agent contests with evidence gets a reply on the pull request.

After `--max-repairs` repair rounds (default 3) or 5 merges of the base
branch, the next repair parks the pull request instead: the loop says so in one
comment and changes nothing more on it. Answering a person is not a repair: the
loop answers while the pull request is not parked, and parks it when a comment
is unclear, asks for more than a change within its scope, or asks for a change
that does not pass, or when the publication check withholds its reply. A
parking comment gives a fixed reason, never local output; one that fails to
post is retried before anything else is done on that pull request.

A pull request is merge-ready when it is not a draft, at least one check ran
at its head and every check there passed, the review bot's own included (a
queued re-run counts as running), every check the base branch's rulesets or
protection require has run, from the GitHub App they name if they name one, the
checks are those of the head commit, the review bot (when the profile names one)
approved the head, no person's comment waits, and GitHub reports it mergeable. The loop then releases the task's claim
and leaves the pull request to the maintainers; the open pull request remains
the task's public claim.

## Several workers and stops

`--workers N` runs N worker processes that share one state directory; separate
commands with the same `--state-dir` share it too. Each worker process holds
the lowest free slot `K` while it runs, which names it `<login>-<site>-wK`
(`<site>` a random name the state directory keeps), its worktree and its
claims; a worker that starts later takes over a free slot and releases the
claims its previous holder left without a pull request or pushed branch.
Attempts under way and pushed branches count towards `--max-open-prs`, checked
under a lock against a fresh read. `--agent`
repeated gives the workers different agents, worker K taking the K-th,
cyclically. A worker that crashes is restarted at most five times.

A worker stops when `STOP` exists in the state directory (every worker) or in
`workers/<id>/` (that worker), after its current round; on SIGINT or SIGTERM,
stopping its agent and releasing the claim of an unfinished attempt; once
`--budget` dollars are spent in this run (Claude Code's reported cost; Codex
runs cost `--price` per million tokens, required with `--budget`; a run that
reports no cost counts as `--attempt-budget` when Claude Code enforced that cap,
and otherwise stops every worker of the run for good, no agent starting after
that); after
`--max-hours`; and after 5 failed rounds in a row. With `--max-attempts N` it
authors at most N tasks; with `--loop` it then follows their pull requests and
exits once none needs work. A rate limit pauses authoring for 15 minutes and
resumes the agent's session.

`autoform work status [--json]` reports spend, attempts, outcomes and pull
request state. It resolves `origin` as `work run` does (a fork clone's source).
Pass `work status` the same `--checkout`, `--repo` or `--state-dir` you gave
`work run`. JSON (`autoform-work-status/v1`) holds `state_dir`, `spent_usd`,
`attempts`, `author_outcomes` (outcome to count), `stop_file` (path or `null`)
and `pull_requests` with `number`, `url`, `task`, `branch`, `agent`, `opened_at`,
and `repairs`, `merges`, `ready_head`, `parked` and `closed` (each `null` until set).

## The state directory

`--state-dir`, by default `$XDG_STATE_HOME/autoform/work/<owner>__<repo>`
(`~/.local/state/...`), holds `ledger.jsonl` (one line per event: claims, agent
runs with their cost, pushes, rounds, stops), `prs/<number>.json`,
`failures.jsonl`, `pending/` (pushed branches without their pull request),
`releases/` (claim releases to retry), `attempts/`, `facts/`, `cache/`, and
`workers/<id>/` with the worktree and the agent transcripts.

## Profiles

A profile, `autoform-work-profile/v1` TOML, says what the loop knows about a
project. The first found is used: `--profile FILE`, the checkout's
`.autoform/work.toml`, the profile AutoformBot ships for the repository
(facebookresearch/atlas-lean has one), else the defaults. Every key is optional:

- `[tasks]`: `command`, run from the checkout, printing the tasks as
  `autoform work list --json` does, or a picker's `{"next": [...], "tasks": ...}`
  of free tasks; `tasks_option`, how `--tasks FILE_OR_URL` is passed to it;
  `taken_module`, a Python file of the checkout whose `taken(items, pulls)`
  says which tasks a pull request takes; `phases`; `claims_repo`.
- `[verify]`: `commands`, run in the worktree; `publish_check`, run on the
  commits and the exact branch, title and body with `{base}`, `{head}` and
  `{metadata}` (a JSON file) filled in, and `publish_check_public`, the option
  that passes each commit already public; `statement_fidelity` (default true);
  `forbidden`, more regular expressions for added Lean lines.
- `[pull_request]`: `branch_prefix`, `body_lines`, `blueprint`.
- `[review]`: `bot`, the reviewer's login; `markers`, text its verdicts carry;
  `checks`, the checks its workflow runs.
- `[agent]`: `instructions`, a text file beside the profile added to every
  prompt.

The agent runs with the permissions of your account and without a sandbox; the
missing credentials keep honest mistakes off GitHub, not a hostile agent. Run
the loop under an account or in a container holding nothing else you value.
