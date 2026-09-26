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
serve the built site over localhost and provide the overview, progress, project
graph, relevant chapter graph, and node-neighborhood links.

Guide the review from coarse to fine: declared scope and exclusions, milestone
book, progress summary, cross-chapter graph, chapter graph, then individual node
and Lean-source links. Record each human decision as `approve`, `revise`, or
`block`, with the exact page or node and rationale. Separate validator output
from the person's judgment. Do not silently apply requested revisions: hand
mathematical-plan changes to Roadmap, Lean implementation changes to
Orchestrate, and autonomous rubric scoring to Agent Review.

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
   in the CLI reference. Every `origin: cited` Lean article must identify an
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
   one extraction then serves every card instead of one per card. Never
   hand-author or repair a card's path, hashes, or frontmatter.
4. Audit and render from that same bundle, then strict-build the site. Both
   commands re-extract current evidence and fail closed if the bundle is stale,
   incomplete, or inconsistent. Every formalized statement gains a *Review*
   disclosure showing the exact hashed packet, its read-back, the article and
   source evidence it is bound to, and the approval state.
5. Walk the person through each statement: source and book text first, then the
   read-back, then the exact packet. Ask whether the read-back says what the
   book says, whether any hypothesis is missing or added, and whether the
   definitions mean what the book's do. When they approve, copy the complete
   per-article review hash shown by the validated review view into
   `review_approved`. That hash binds the article title and statement, cited passage,
   exact packets, and current read-backs. This edit is the person's assertion,
   not the agent's. When they do not approve, record `revise` with their reason
   and hand the change to Roadmap or Orchestrate.
6. Run the review-aware audit again before reporting. Treat missing testimony,
   unresolved extraction, an incomplete bundle, any packet or read-back
   mismatch, and any approval drift as failures. An edit to any reviewed input
   must invalidate the approval it changed.
