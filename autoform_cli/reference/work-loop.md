# The work loop

`autoform work run` contributes to a Lean project unattended: it claims a ready
task, has a coding agent formalize it, checks the result itself, opens a pull
request, and follows the pull request's checks and reviews until a maintainer
could merge it; `work --loop` is short for `work run --loop`. The [CLI
reference](../README.md#work-run) gives the synopsis.

## Requirements and authority

Run it in a clone of the project with Lean and Lake, after `lake build` (in a
Mathlib project, `lake exe cache get` first), from an AutoformBot checkout or
plugin install, so the [Formalize skill](../../skills/formalize/SKILL.md) is
there. It needs Git with `user.name` and `user.email` set, the GitHub CLI `gh`
logged in as the account that opens the pull requests, and Claude Code
(`claude`) or Codex (`codex`) logged in; it runs on Linux and macOS.

Running it is your authority to push claims, branches and pull requests as
your `gh` account, and to spend on the agents. It pushes only branches under
the profile's prefix (`autoform-work/` by default) of your fork, which it
creates when you have none, or of `--fork OWNER/NAME`. It never pushes the
base branch, merges, approves, enqueues, labels other pull requests, or
comments anywhere but on its own pull requests. `--dry-run` stops each attempt
before its first push and keeps the commit in the worker's worktree.

## Rounds

Without `--loop` a worker does one round; with it, rounds repeat until a stop
(below). A round does one unit of work, the first that applies:

1. Maintain one of the loop's open pull requests, oldest first, one worker per
   pull request: answer a person's comment; merge the base branch into a
   conflicting branch; address the review bot's requested changes on the head;
   repair a failed check.
2. Otherwise, below `--max-open-prs` (default 5), author a new task.

A pull request is the loop's when you opened it and its body holds the
loop's hidden `<!-- autoform-work {...} -->` marker. A GitHub error ends the
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
   read ends the round. The lease note names the worker, its host, the task
   and, once opened, the pull request.
3. **Prepare.** The worker's worktree, `workers/<id>/worktree` in the state
   directory, is reset to the base branch's tip. Its `.lake/packages` links to
   the checkout's while both manifests are equal, and its build starts as a
   copy-on-write copy of the checkout's. For a proof task, the harness locates
   each target declaration, builds its module and records its statement facts.
4. **Agent.** The agent gets the [prompts](../work_loop/prompts/), the task
   item, the path of the Formalize skill's one-article procedure, and the
   profile's instructions, with `--claude-arg` or `--codex-arg` added to its
   command line. It runs without GitHub credentials (no `GH_TOKEN`,
   an empty `GH_CONFIG_DIR`) and is told never to commit or push. It writes a
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
   `Quot.sound`; and the profile's commands pass. A failure goes back to the
   agent twice at most.
6. **Publish.** The harness commits as you (with a `Co-authored-by` line naming
   the agent, unless `--no-co-author`), refuses a title or body that would take
   another task, runs the profile's publication check, reads the task list
   again and gives up if another pull request took the task, then pushes the
   branch create-only and opens a ready pull request. Its body is the agent's,
   then `Tasks: <article id>`, the profile's lines, `Autoform project:
   <owner>/<repo>/<blueprint>`, a line saying the loop opened it, and the marker.
   A branch pushed without its pull request is opened ten minutes later.

An attempt that ends without a pull request releases its claim.

## Following a pull request

Each round reads the loop's pull requests from GitHub, which is the source of
truth. The review bot's verdict is its latest approving or change-requesting
review carrying one of the profile's markers; it counts only for the commit it
reviewed. Cancelled checks, checks waiting for a maintainer (a first-time
contributor's workflow runs), checks the profile names as the review bot's, and
checks that fail on the base branch too are never repaired. A repair is
verified like an attempt, committed on the branch and pushed leased on the head
it started from, never force-pushed. A merge of the base branch commits a merge.
A finding the agent contests with evidence gets a reply on the pull request.

After `--max-repairs` repair rounds (default 3) or 5 merges of the base
branch, the next repair parks the pull request instead: the loop says so in one
comment and changes nothing more on it. Answering a person is not a repair: the
loop answers while the pull request is not parked, and parks it when a comment
is unclear or asks for more than a change within its scope.

A pull request is merge-ready when it is not a draft, every check has passed
at its head and every required check of the base branch's rules has run, the
review bot (when the profile names one) approved the head, no person's comment
waits, and GitHub reports it mergeable. The loop then releases the task's claim
and leaves the pull request to the maintainers; the open pull request remains
the task's public claim.

## Several workers and stops

`--workers N` runs N worker processes that share one state directory; separate
commands with the same `--state-dir` share it too. `--agent` repeated gives the
workers different agents, worker K taking the K-th, cyclically. A worker that
crashes is restarted at most five times.

A worker stops when `STOP` exists in the state directory (every worker) or in
`workers/<id>/` (that worker), after its current round; on SIGINT or SIGTERM,
stopping its agent and releasing the claim of an unfinished attempt; once
`--budget` dollars are spent in this run (Claude Code's reported cost; Codex
runs cost `--price` per million tokens, required with `--budget`); after
`--max-hours`; and after 5 failed rounds in a row. With `--max-attempts N` it
authors at most N tasks; with `--loop` it then follows their pull requests and
exits once none needs work. A rate limit pauses authoring for 15 minutes and
resumes the agent's session.

`autoform work status [--json]` reports the state directory: spend, attempts,
their outcomes, and each pull request's state.

## The state directory

`--state-dir`, by default `$XDG_STATE_HOME/autoform/work/<owner>__<repo>`
(`~/.local/state/...`), holds `ledger.jsonl` (one line per event: claims, agent
runs with their cost, pushes, rounds, stops), `prs/<number>.json`,
`failures.jsonl`, `facts/`, `cache/`, and `workers/<id>/` with the worktree and
the agent transcripts.

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
