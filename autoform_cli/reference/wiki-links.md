# Wiki reference layer

The viewer derives optional `dependencies/wiki-links.json`, schema `autoform-wiki-links/v1`,
from captured article bytes. This is internal viewer data, not a public CLI contract. Its `references`
are ordinary authored Markdown links between published mathematical articles,
not statement or proof prerequisites. Source tracks, code and TeX literals,
provenance sections, and ancestor/descendant placement links are excluded.
Unsupported HTML/code/math or publication-as-literal omissions make outgoing
links unavailable, not zero. Known incoming links remain valid; inline spans stay within paragraphs.

Wire endpoints use the published node array, like compact parent/dependency references restored to IDs on load.
`index_sha256` binds them to that exact index's UTF-8 bytes; readers must verify
the digest before using them. `availability`, `unavailable_sources`, and an
optional `reason` distinguish a genuinely empty layer from unavailable data.
The separate asset has a 32 MiB limit. An oversized layer is explicitly omitted
with a warning, never silently truncated. Neither wiki references nor their
availability change the formal DAG, readiness, proof status, or index bytes.

Private controller hooks coalesce loads and normalize verified offsets to canonical article IDs.
Viewer affinities group intact formal components, never redefine proof edges, ranks or SCCs.
