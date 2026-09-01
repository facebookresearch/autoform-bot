# Goal prompt: execute the archive skill transport PR series

Execute the repository-local plan in
`ARCHIVE_SKILL_TRANSPORT_PLAN.md`. Deliver accepted work as the individual pull
requests defined in its **Delivery contract** and **Planned PR series**. Do not
combine the program into one implementation branch or one umbrella PR.

## Authorization boundary

This goal authorizes you to create local branches and commits, push those
branches to the confirmed intended GitHub repository or the authenticated
user's fork, and open draft pull requests. It does not authorize merging pull
requests, enabling publication, contacting people, changing repository
settings, using private services, or copying archive material whose reuse is
not authorized and license-compatible.

Before the first push, verify the current checkout, canonical repository,
authenticated GitHub identity, available push remote, branch protection/base,
and dirty worktree. Preserve unrelated changes. If the authenticated account
cannot push to the canonical repository, use its existing fork or create the
normal GitHub fork required to open the requested PRs; do not rewrite an
unrelated remote.

## Execution rule

Work on the next incomplete, dependency-ready PR in the plan:

1. Read the complete plan row, applicable repository instructions, and relevant
   existing code before editing.
2. Confirm that prerequisite PRs are merged or select the documented stacked
   base. Do not duplicate parent commits in nominally independent PRs.
3. Create a branch named `autoform/<pr-id>-<short-slug>` from the correct base.
4. Implement only that PR's review unit and explicit prerequisites.
5. Add meaningful positive and negative tests for its observable behavior.
6. Run the focused tests and all cross-cutting gates required by the plan.
7. Inspect the full diff, generated files, internal-token scan, and repository
   status before committing.
8. Commit the coherent change, push the branch, and open a draft PR containing:
   scope, non-goals, dependency/base information, test commands and results,
   risks, migration impact, and rollback notes.
9. Record the PR URL and head/base commits locally in the goal report. Never
   claim a PR exists without verifying it through GitHub.
10. Continue to another PR only when its base and dependencies are valid and
    its work can proceed without prejudging review of an unresolved contract.

If a PR changes a contract consumed by later work, stop that stack after
opening the contract PR unless the plan explicitly allows a stacked child. A
stacked child must target its parent branch until the parent merges, then be
rebased or retargeted without duplicating commits.

## Source and licensing rule

The archive is design evidence, not automatically licensed source code. Its
verified identity is recorded in the plan. Until authorization and license
compatibility are documented, independently implement portable behavior from
the stated requirements. Never copy internal endpoints, credentials, employee
identifiers, machine paths, model entitlements, private benchmark details, or
service-specific operational commands.

## Branch boundaries

- P-series PRs target `main`.
- E-series PRs target `execution` after synchronizing their accepted core
  prerequisites.
- C-series PRs require an explicitly approved companion plugin/repository; do
  not create that external repository merely because the plan names it.
- D01 is documentation-only and waits for stable interfaces.
- `COMPOSE`, `EXCLUDE-*`, and project-specific inventory decisions do not
  receive implementation PRs.

The first eligible unit is P00. P01–P03 implement the detailed prompt in
`FORMALIZATION_QUALITY_GOAL.md`. Do not start E- or C-series work merely because
P-series branches have been opened; follow the dependency and approval rules.

## Quality and stopping rules

- Preserve `main` as non-autonomous.
- Keep Markdown as the authored project state; do not introduce a competing
  mutable database.
- A model's success report is never test evidence.
- Do not weaken Lean builds, kernel checks, axiom checks, path containment, or
  source-fidelity requirements.
- Never turn a timeout, unavailable dependency, uncertain verdict, or missing
  source into a pass.
- Do not mark a PR ready while required tests fail.
- Do not open placeholder PRs for future plan rows.
- Do not merge any PR under this goal.

When genuinely blocked, preserve the working branch, report the exact repeated
blocker and evidence, and do not broaden authority or silently change the
planned architecture.

## Completion

The goal is complete only when every approved, non-excluded plan row has either:

- a verified PR URL with its intended base, passing evidence, and dependency
  status; or
- an explicit terminal disposition explaining why the plan says no PR should
  be created or why further user/reviewer approval is required.

Finish with a PR ledger containing PR ID, title, URL, head, base, status,
dependencies, tests, and blockers. Distinguish draft/open/merged status from
local completion, and never merge as part of this goal.
