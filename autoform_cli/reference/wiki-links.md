# Wiki reference layer

Rendering from captured article bytes also writes the optional derived asset
`dependencies/wiki-links.json`, schema `autoform-wiki-links/v1`. Its `references`
are ordinary authored Markdown links between published mathematical articles,
not statement or proof prerequisites. Source tracks, code and TeX literals,
provenance sections, and ancestor/descendant placement links are excluded.
Unsupported HTML or unclosed code/math blocks make outbound extraction
explicitly partial; known incoming links to a published article remain valid.

Reference endpoints are offsets in the unchanged search-index node array.
`index_sha256` binds them to that exact index's UTF-8 bytes; readers must verify
the digest before using them. `availability`, `unavailable_sources`, and an
optional `reason` distinguish a genuinely empty layer from unavailable data.
The separate asset has a 32 MiB limit. An oversized layer is explicitly omitted
with a warning, never silently truncated. Neither wiki references nor their
availability change the formal DAG, readiness, proof status, or index bytes.

Navigation's optional `loadWikiLinks()` coalesces requests; `getWikiLinks()` and
`onWikiLinks(state)` expose verified canonical references or explicit unavailability.
