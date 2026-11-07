# Explorer ownership and failure model

The mathematical reader and the whole-project map share one page. Old dependency-page URLs
redirect to canonical reading pages, not a second renderer. Payloads/controller hooks are internal, not a public viewer API.

## Evidence and publication

Captured `blueprint/roadmap/` Markdown owns articles and the typed, weighted
formal DAG. Authored containment and areas organize navigation, not prerequisites.
Optional authored wiki links associate intact formal components, never ranks, readiness or proof status.

[render.py](../autoform_cli/ui/render.py) retains source captures and assembles safe
reading pages; rereading its private staged pages is not rereading authored input.
[graph_pages.py](../autoform_cli/ui/graph_pages.py) owns canonical paths and contexts.
[site_index.py](../autoform_cli/ui/site_index.py) writes the search index, with typed relations, and wiki links;
[dag_viewer.py](../autoform_cli/ui/dag_viewer.py) writes what each scope's list shows. No consumer-name,
filesystem-derived taxonomy, or workspace/schema exception belongs here.

## Navigation and failure

[blueprint-navigation.js](../autoform_cli/ui/assets/blueprint-navigation.js) owns bounded same-origin fetches, coalesced
index loading, exact-byte digests, request epochs/aborts, caches and the versioned history namespace. The book's map
reads through `fetchJSON` (`autoformFetchJSON` on its host): the same fetch checks, but no cache, epoch or `onError`.
Index batches yield to visible frames with a timer fallback; validation publishes no partial index.
Parents/relations decode index offsets to IDs; wiki links bind the exact wire SHA. Unavailable is not zero.
Directory aliases preserve statement fragments without weakening exact resource/canonical validation.
Fetched HTML must come from a compatible publishable producer. Fragment copying,
active-tag removal and URL rebasing are defense in depth, not a generic sanitizer.

Preparation failures precede route commitment. `onCommit` runs after URL/history
commit and may already have changed DOM when it rejects: no rollback is promised.
Rejection blocks snapshot capture while failed and clears the private snapshot cache.
Clearing the active browser snapshot is attempted; extra in-flight checkpoints
or failed history invalidation quarantine browser snapshots for this router instance.
Fresh private snapshots remain usable; the renderer rejects known route-owner
mismatches. A new-tab canonical article provides fresh server-rendered reading.

## Rendering and mathematics

[blueprint-dag.js](../autoform_cli/ui/assets/blueprint-dag.js) owns the outline, search, status
filter, statement lists and reader state; its stage hosts [project-map.js](../autoform_cli/ui/assets/project-map.js).
On the book's host, `data-map-start` names the page's node or scope, `data-map-statuses` the
statuses the filter keeps (the map dims the rest), `bp-dag-has-selection` a selected result, and
`bp-dag-resizing` a pane drag; a pick on the map is an `autoform-map-select` event, and the book owns the
address and history. A map that never marks its host `data-pm-ready` leaves a
notice; outline, search and lists still work. A page whose reader only lists its children opens
map-first, as chapters and Home do when a phone lands on them; statements open in the reader.
Server-produced pages contain complete mathematical reading and theorem anchors without
JavaScript; enhancement uses published fragments, never graph summaries.

[mathjax.py](../autoform_cli/ui/mathjax.py) owns marked formulas: ordinary reading uses
project macros; read-back cards use fresh isolated inputs without them. Reading
labels carry converter-derived text/code/TeX parts, revalidated by the client;
Code and unsupported previews stay literal; deferred list math is bounded and viewer-scoped.
Page-owned reading labels add no MathJax menus/tab stops.

## Builds and verification

[build-explorer.mjs](../scripts/build-explorer.mjs) hashes canonical UTF-8/LF development text.
That fingerprint differs from raw math sources, exact index/sidecar bytes and the runtime byte SHA.
Pinned development-only compilers ship one asset below 145 KB; consumers need no Node.
Unsafe compression, public-field mangling/raw-source fallbacks are forbidden; stale bundles fail publication.

Source/unit tests check lifecycle, routing and focus; MathJax/WebKit check glyphs, the map's selection, focus and Back.
Whole-Atlas qualification checks corpus preservation, strict publication and UI timing—not kernel certification.
