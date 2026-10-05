---
name: human-review
description: >-
  Prepare and guide human inspection of an Autoform roadmap or formalization
  through its Obsidian graph and rendered blueprint site. Use when a person wants
  to browse, approve, reject, or discuss scope, dependencies, progress, source
  links, or Lean artifacts visually; do not substitute an autonomous agent
  verdict for the human's judgment.
---

# Prepare a human review

Inspect the repository without changing mathematical content. Require an
existing Autoform vault and site configuration; hand missing infrastructure to
Setup. Keep the Markdown vault as the source of truth and regenerate only
derived review views.

Regenerate the review views from `<PROJECT>`: validate the blueprint, refresh
the Mermaid graph with `autoform-visualize` so Obsidian shows current
dependencies, render the site source, then strict-build the site. Follow the
publication sequence in the [CLI reference](../../autoform_cli/README.md#commands),
but omit `--require-declarations`: review happens while statements are still
unformalized, and a missing declaration is something for the reviewer to see
rather than a reason to refuse to render.

Stop on structural failures and present them before asking for mathematical
judgment. For vault review, point the user to `blueprint/README.md`, coverage,
chapter pages, and `blueprint/dependencies.md` in Obsidian. For browser review,
run `autoform dashboard <PROJECT> --site-dir site` so the same site deployed to
GitHub Pages is served on loopback with local-only live claim badges. Provide
the overview, including its progress summary, plus the project graph, relevant
chapter graph, and node-neighborhood links.

Guide the review from coarse to fine: declared scope and exclusions, milestone
book, landing-page progress summary, cross-chapter graph, chapter graph, then
individual node and Lean-source links. Record each human decision as `approve`, `revise`, or
`block`, with the exact page or node and rationale. Separate validator output
from the person's judgment. Do not silently apply requested revisions: hand
mathematical-plan changes and Lean implementation changes to Roadmap, which
reopens the affected articles for Formalize, and autonomous rubric scoring to
Agent Review.

Treat the landing page's `Scoped roadmap` percentage as completion among
formalizable leaf targets that are fully proved, including every dependency
recursively. This requires proofs for theorems and bodies for definitions. A
target marked `mathlib: true` follows the authored status contract; the marker
is an author assertion, not audit verification that the declaration is in
Mathlib. Treat the percentage never as whole-source completion. Read the
adjacent declared source coverage and its linked coverage contract before
making scope claims. A statement-only theorem remains incomplete whether it is
blocked or ready to prove.

## Review formalized statements through prepared evidence and read-backs

A compiled proof says nothing about whether the statement means what the book
says. The person reviews a *prepared review bundle*: the current article
statement and cited passage, the elaborated Lean signature, and the project
definitions it rests on. Beside that evidence they read an independent
*read-back*, a blind rendering in mathematical English of what one exact Lean
packet literally asserts. The kernel covers everything below this surface.

Use the `review` commands and review-bundle flags documented in the
[CLI reference](../../autoform_cli/README.md#commands). Do not substitute a
saved skeleton report: preparation, recording, auditing, and rendering each
check that the evidence still describes the current blueprint and built Lean
tree.

1. Confirm that the repository has opted into enforcement with the versioned
   `blueprint/.autoform-review` policy marker. Require durable `article_id`
   metadata for every Lean-mapped article, using the article-ID migration check
   in the CLI reference. Every Lean-mapped article must also be a
   declaration-sized leaf, naming a `declaration:` kind and containing no other
   articles, so its evidence appears in its rendered box; review commands refuse
   any other with `review-article-shape`. Every `origin: cited` Lean article must identify an
   exact line range in a local, non-Markdown source snapshot. Build the Lean project, then prepare a versioned review
   bundle and its blind packets. Keep the bundle and its identity manifest with
   the coordinator. A packet is the only input that crosses the blind-review
   boundary; its opaque filename must not reveal the article or declaration.
2. Obtain a read-back for every packet that has none or whose card is invalid.
   Never write one yourself: you know what the code is meant to say. Launch an
   independent sub-agent in a fresh workspace containing only that packet and
   [the read-back reference](references/readback.md). Do not give it the
   repository, article, source passage, bundle manifest, or a revealing task
   description. Ask for testimony only.
3. Back in the coordinator's workspace, record the testimony through the CLI.
   Pass the prepared bundle and the exact packet file the agent read. The
   command resolves the durable article ID, re-extracts the current Lean
   evidence, rejects a stale bundle or changed packet, and writes the vault
   card. With several testimonies, record them as one batch from a manifest:
   one extraction then serves every card instead of one per card. Record while
   the blueprint is idle: an edit to any article during the extraction aborts
   the record with nothing filed, and running it again completes it. Never
   hand-author or repair a card's path, hashes, or frontmatter.
4. Audit and render from that same bundle, then strict-build the site. Both
   commands re-extract current evidence and fail closed if the bundle is stale,
   incomplete, or inconsistent. Every formalized statement gains a *Review*
   disclosure showing the exact hashed packet, its read-back, the article and
   source evidence it is bound to, and the approval state. A current approval
   reads self-approved until a verifier names the person who approved it.
5. Walk the person through each statement: source and book text first, then the
   read-back, then the exact packet. Ask whether the read-back says what the
   book says, whether any hypothesis is missing or added, and whether the
   definitions mean what the book's do. When they approve, copy the complete
   per-article review hash shown by the validated review view into
   `review_approved`. That hash binds the article title and statement, cited passage,
   exact packets, and current read-backs. This edit is the person's assertion,
   not the agent's, but the hash only shows that nothing changed since it was
   written, and anyone can copy it. Commit the approval in a pull request into
   the default branch, from a fresh branch of the project's own repository
   (not a fork), that changes only articles and read-back cards and whose diff
   adds that `review_approved` line. Once `review check` is green on its final
   head commit, ask an individual `@user` code owner of the article with write
   access, who neither opened the pull request nor wrote any of its commits,
   to approve that head; a later push needs a new approval. The default
   branch's site names them instead of saying self-approved only when the
   default branch has a ruleset requiring code owner review, dismissal of
   stale approvals, and approval of the most recent push, `CODEOWNERS` owns
   every path, that pull request, merged, recorded the hash, the reviewer
   is a code owner both before the pull request and on the default branch, and
   `autoform-verify.yml` passed on the approved head. Anything else, a moved
   article or a pull request that also changes other files included, reads
   self-approved with the reason. To re-approve such a hash, a later pull
   request that changes only that article rewrites its `review_approved` line
   (moving it within the frontmatter is enough) and is reviewed as above. To
   withdraw an approval, dismiss the review. A dismissal starts no Pages
   build, so the site reads self-approved only after the next one; the Pages
   workflow's hourly schedule rebuilds a day after the last build, so that is
   within about a day and an hour when the rebuild succeeds, and later when it
   fails or when GitHub delays the schedule. The schedule builds only while at
   most 100 of the hour's API requests are spent, so in a repository whose
   other runs (gate runs, other workflows) keep more spent it never builds.
   Run the Pages workflow by hand (`workflow_dispatch`) to show it at once. The
   `autoform-review-gate.yml` check on the pull request is early feedback; the
   Pages label decides. The
   [CLI reference](../../autoform_cli/README.md#commands) states the full rule. When
   the person does not approve, record `revise` with their reason and hand the
   change to Roadmap.
6. Run the review-aware audit again before reporting. Treat missing testimony,
   unresolved extraction, an incomplete bundle, any packet or read-back
   mismatch, and any approval drift as failures. An edit to any reviewed input
   must invalidate the approval it changed. Do not report a statement as
   approved by a person while its label says self-approved.
