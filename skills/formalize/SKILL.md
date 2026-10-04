---
name: formalize
description: >-
  Formalize ready leaves from an existing Autoform Markdown roadmap in Lean,
  using native agents, fail-closed claims, and verified Markdown progress.
  Use for proof execution; send missing scope or DAG structure to Roadmap.
---

# Formalize the Markdown roadmap

Treat `blueprint/roadmap/**/*.md` as the sole durable work graph. Start by
running `autoform work list <PROJECT> --lean-root <PROJECT>` and inspect a
selected leaf with `autoform work context <NODE> <PROJECT> --lean-root
<PROJECT>`. The returned phase is derived from typed dependencies and verified
assertions; never author ready, running, retrying, failed, or blocked scheduler
states.

For a direct formalization request, use a compatible native persistent Goal
when available and complete one useful frontier pass. Native subagents may work
independent leaves in separate Git worktrees, but no custom scheduler or
provider adapter is part of the protocol.

Before editing, set `AUTOFORM_WORKER_ID` and acquire the exact `claim_target`
returned by `work context`. A failed acquire or renewal means ownership is
unproven: stop writing. Keep the claim through verification and release it on
every outcome. Serialize shared Lake builds with the `lake-build` resource
claim.

After acquiring the claim, reload `work context` and require the same phase,
dependency frontier, and `article_revision` before editing. Before publishing,
confirm that the claim is still held, dependency readiness is unchanged, and
the selected article differs from that revision only by this worker's intended
edits. Unrelated parallel articles may legitimately change the graph-wide source
revision.

Read the complete article, cited sources, dependency articles, and existing Lean
target. Preserve the exact mathematical statement. Search the pinned Mathlib
checkout before adding helpers, and use the shared Lean LSP and REPL with the
absolute project path. Finish with the focused Lake target; do not accept
`sorry`, new axioms, unsafe shortcuts, a weaker theorem, unused hypotheses, or
an unrelated declaration.

On success, update only the claimed article with the exact compiled declaration
and truthful `statement: formalized` or `proof: formalized` assertions. On a
useful failed route, record only distilled reusable evidence under
`## Execution notes`—the remaining goal, checked lemmas, and next route—never a
transcript or retry counter. A missing prerequisite or incorrect decomposition
returns to Roadmap instead of silently changing the DAG.

Run `autoform check` and `autoform audit`, re-read the work frontier, and repeat
while independent ready leaves and authorized capacity remain. Stop when the
frontier is empty or every remaining attempt has a concrete mathematical or
ownership blocker. Report changed articles and Lean files, claims released,
checks run, and exact remaining goals.
