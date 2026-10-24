---
name: human-review
description: >-
  Prepare and guide human inspection of an Autoform roadmap or formalization
  through its Obsidian vault and rendered blueprint site. Use when a person wants
  to browse, approve, reject, or discuss scope, dependencies, progress, source
  links, or Lean artifacts visually; do not substitute an autonomous agent
  verdict for the human's judgment.
---

# Prepare a human review

Inspect the repository without changing mathematical content. Require an
existing Autoform vault and site configuration; hand missing infrastructure to
Setup. Keep the Markdown vault as the source of truth and regenerate only
derived review views.

There is no `autoform` on `PATH`. Run it from `<PROJECT>` as
`uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform`, and `autoform-visualize`
the same way, where the root is the absolute plugin directory holding `skills/`
and `pyproject.toml`; give a subagent that prefix.

Regenerate the review views from `<PROJECT>` with the publication sequence in
the [CLI reference](../../autoform_cli/README.md#commands), but omit
`--require-declarations` so the reviewer sees missing declarations.

Stop on structural failures and present them before asking for mathematical
judgment. For vault review, point the user to `blueprint/README.md`, coverage,
chapter pages, and the status and chapter summary in `blueprint/dependencies.md`
in Obsidian. For browser review, run
`autoform dashboard <PROJECT> --site-dir site`, which serves the deployed site
on loopback with local-only live claim badges, and give the overview plus the
relevant project, chapter, nested-scope, and statement-context links. Every
generated site route uses the same Canvas plus semantic-DOM viewer. Drill from
the project's topics to a chapter, a nested scope, then a searched statement, in
a window wider than 1,100 px so open details leave the hierarchy rail visible;
at project scale, select one topic to see its dependency neighborhood. A level
with no authored dependency edges says so.

Guide the review from coarse to fine: declared scope and exclusions, milestone
book, landing-page progress summary, cross-chapter graph, chapter graph, then
individual node and Lean-source links. Record each human decision as `approve`,
`revise`, or `block`, with the exact page or node and rationale. Separate
validator output from the person's judgment. Do not silently apply requested
revisions: hand mathematical-plan changes and Lean implementation changes to
Roadmap, which records the decision and retracts the affected article so
Formalize takes it up under the
[revision contract](../../autoform_cli/reference/claims-and-revisions.md#revision-contract),
and autonomous rubric scoring to Agent Review.

Treat the landing page's `Fully proved` percentage as the share of
formalizable leaf targets that are fully proved, as the
[derived status](../../autoform_cli/README.md#assertions-and-derived-status)
defines it, never as whole-source completion; read the declared source coverage
in its linked coverage contract before making scope claims. A target marked
`mathlib: true` follows the authored status contract; the marker is an author
assertion, not audit verification that the declaration is in Mathlib. A
statement-only target is incomplete, and so is a conditionally proved one,
whose proof rests on an open statement: present it as conditional, naming the
open statements in its `Assumes` row, never as proved or sorry-free.

## Review formalized statements through blind read-backs

The person compares the article statement and cited passage, the elaborated
Lean signature, and the project definitions it rests on with a *read-back*: a
blind rendering in mathematical English of what one exact Lean packet literally
asserts. Use the `review` commands in the
[CLI reference](../../autoform_cli/README.md#commands); `review check` is the
one judge of whether cards and approvals are current.

1. Confirm that the repository has opted in with the
   `blueprint/.autoform-review` policy marker, the line
   `autoform-review-policy/v1`. `autoform init` never writes it; if it is
   missing, ask the user before committing it, since it makes CI enforce
   statement review. Every Lean-mapped article needs durable
   `article_id` metadata (see the article-ID migration check in the CLI
   reference) and must be a declaration-sized leaf, naming a `declaration:` kind
   and containing no other articles; review commands refuse any other with
   `review-article-shape`. Every `origin: cited` Lean article must cite an exact
   line range in a local, non-Markdown source snapshot. Build the Lean project,
   prepare the blind packets, and keep their manifest with the coordinator.
2. Obtain a read-back for every packet that has none or whose card
   `review check` reports invalid. Never write one yourself: you know what the
   code is meant to say. Launch an independent sub-agent in a fresh workspace
   containing only that packet, under its opaque hash filename, and
   [the read-back reference](references/readback.md). Do not give it the
   repository, article, source passage, packet manifest, or a revealing task
   description. Ask for testimony only.
3. Back in the coordinator's workspace, record the testimony through the CLI
   with the packet directory, the article ID, and the declaration. Never
   hand-author or repair a card's path, hashes, or frontmatter.
4. Run `review check`; for each missing or stale card, prepare again and repeat
   steps 2 and 3. Then render with `--review` and strict-build the site, which
   adds a *Review* disclosure under every formalized statement.
5. Walk the person through each statement: source and book text first, then the
   read-back, then the exact packet. Ask whether the read-back says what the
   book says, whether any hypothesis is missing or added, and whether the
   definitions mean what the book's do. When they approve, copy the review hash
   `review check` prints for the article into `review_approved`. This edit is
   the person's assertion, not the agent's, but the hash only shows that nothing
   changed since it was written, and anyone can copy it. Commit the approval in
   a pull request into the default branch and ask a code owner of the article to
   approve it; a later push needs a new approval. When the person does not
   approve, record `revise` with their reason and hand the change to Roadmap.
6. Run `review check` again before reporting, and treat each finding as a
   failure. Do not report a statement as approved by a person while its label
   says self-approved.
