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

There is no `autoform` on `PATH` in a Lean project. Run every `autoform` and
`autoform-visualize` command, from `<PROJECT>`, as
`uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform` followed by its arguments
(with `autoform-visualize` in place of `autoform` for the graph), where
`<AUTOFORM_PLUGIN_ROOT>` is the absolute plugin root: the directory that holds
this skill's `skills/` folder, along with `pyproject.toml` and `autoform_cli/`.
Give a subagent that root and this prefix with its task.

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
records the decision and retracts the affected article so Formalize takes it up
under the [revision contract](../../autoform_cli/README.md#revision-contract),
and autonomous rubric scoring to Agent Review.

Treat the landing page's `Scoped roadmap` percentage as completion among
formalizable leaf targets that are fully proved, including every dependency
recursively. This requires proofs for theorems and bodies for definitions. A
target marked `mathlib: true` follows the authored status contract; the marker
is an author assertion, not audit verification that the declaration is in
Mathlib. Treat the percentage never as whole-source completion. Read the
adjacent declared source coverage and its linked coverage contract before
making scope claims. A statement-only theorem remains incomplete whether it is
blocked or ready to prove. In a project that allows open statements, a
conditionally proved target, whose proof rests on an open statement without a
recorded Lean proof, is not complete either: it is never fully proved, so it
counts toward the percentage's total but not its completed share, as does
every target that depends on it. Present it as conditional, naming the open
statements in its `Assumes` row, never as proved or sorry-free.

## Review formalized statements through blind read-backs

A compiled proof says nothing about whether the statement means what the book
says. The person reviews the current article statement and cited passage, the
elaborated Lean signature, and the project definitions it rests on. Beside
that evidence they read an independent *read-back*, a blind rendering in
mathematical English of what one exact Lean packet literally asserts. The
kernel covers everything below this surface.

Use the `review` commands documented in the
[CLI reference](../../autoform_cli/README.md#commands). `review check` derives
its evidence from a fresh extraction of the current blueprint and built Lean
tree; it is the one judge of whether cards and approvals are current.

1. Confirm that the repository has opted into enforcement with the versioned
   `blueprint/.autoform-review` policy marker. Require durable `article_id`
   metadata for every Lean-mapped article, using the article-ID migration check
   in the CLI reference. Every Lean-mapped article must also be a
   declaration-sized leaf, naming a `declaration:` kind and containing no other
   articles; review commands refuse any other with `review-article-shape`.
   Every `origin: cited` Lean article must identify an exact line range in a
   local, non-Markdown source snapshot. Build the Lean project, then prepare
   the blind packets. Keep their manifest with the coordinator. A packet is the
   only input that crosses the blind-review boundary; its opaque filename must
   not reveal the article or declaration.
2. Obtain a read-back for every packet that has none or whose card
   `review check` reports invalid. Never write one yourself: you know what the
   code is meant to say. Launch an independent sub-agent in a fresh workspace
   containing only that packet and
   [the read-back reference](references/readback.md). Do not give it the
   repository, article, source passage, packet manifest, or a revealing task
   description. Ask for testimony only.
3. Back in the coordinator's workspace, record the testimony through the CLI
   with the packet directory, the article ID, and the declaration. Never
   hand-author or repair a card's path, hashes, or frontmatter.
4. Run `review check`. It fails on a missing or stale card, an incomplete
   extraction, and any approval that does not match; prepare again and repeat
   steps 2 and 3 for each stale card.
5. Walk the person through each statement: source and book text first, then the
   read-back, then the exact packet. Ask whether the read-back says what the
   book says, whether any hypothesis is missing or added, and whether the
   definitions mean what the book's do. When they approve, copy the review hash
   `review check` prints for the article into `review_approved`. That hash
   binds the article title and statement, cited passage, exact packets, and
   current read-backs. This edit is the person's assertion, not the agent's,
   but the hash only shows that nothing changed since it was written, and
   anyone can copy it. Commit the approval in a pull request into the default
   branch and ask a code owner of the article to approve it; a later push needs
   a new approval. When the person does not approve, record `revise` with their
   reason and hand the change to Roadmap.
6. Run `review check` again before reporting, and treat each finding as a
   failure. An edit to any reviewed input must invalidate the approval it
   changed. Do not report a statement as approved by a person while its label
   says self-approved.
