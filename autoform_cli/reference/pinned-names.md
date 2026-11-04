# Pinned name lists

`blueprint/libraries/` holds pinned name lists, not [library
indexes](search.md#searching-a-library-the-project-depends-on), which hold a
library's whole keyword-declared API. A list, such as a wiki's
`libraries/mathlib.jsonl`, names the declarations a library holds at one pinned
revision, so `check` can warn about a `mathlib_declaration` name the pinned
library lacks. It never earns credit or status, and render never publishes it.

## Format

A list is a `*.jsonl` file directly in the folder, other than a dot file: UTF-8
JSON Lines of at most 64 MiB and 1 MiB a line, with no byte-order mark, no
unknown or missing key, and a final newline. Line 1:

```json
{"record":"header","schema":"autoform-pinned-names/v0","generator":"…","lean_toolchain":"…","coverage":"cited","packages":{"mathlib":{"repository":"https://github.com/leanprover-community/mathlib4","revision":"<40 hex>","source_root":"","docs":"https://leanprover-community.github.io/mathlib4_docs/"}}}
```

`coverage` is `cited`; other values are reserved. A package's `repository` is
https, without `.git` or a final `/`; `source_root`, where module paths start,
is `""` or a relative path ending in `/`, such as core's `src/`; `docs` is
https, ending in `/`, or null. Each later line names a different declaration:

```json
{"record":"name","name":"Finset.sum_range_succ","package":"mathlib","module":"Mathlib.Algebra.BigOperators.Basic","kind":"theorem","line":1230,"signature":"…","deprecated":null}
```

`kind` is `abbrev`, `axiom`, `class`, `constructor`, `def`, `inductive`,
`instance`, `opaque`, `quot`, `recursor`, `structure` or `theorem`; `line` and
`signature` may be null; `deprecated` is null or `{"replacement": name or null}`.

## Warnings

None changes an exit status. A file or header that breaks a rule leaves the list
out, and a bad record is skipped: `check`, `render` and `audit` say why,
counting the records skipped; `audit`'s code is `invalid-pinned-list`. Once a
list holds a name, `check` warns about a `mathlib_declaration` name no list
holds, one that two lists pin at different repositories or revisions, a
deprecated one, and a `mathlib_file` that is not the path of the first name's
module, with or without the package's `source_root`; until then it checks none.
