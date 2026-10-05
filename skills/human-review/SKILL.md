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
