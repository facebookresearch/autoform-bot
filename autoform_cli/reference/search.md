# Autoform search

## Search contract

`autoform search` is a projection of Markdown: it writes nothing, keeps no
index, and starts no process. An article is a hit only when every query term is
a substring of one of its fields, or matches `lean` as below, not necessarily
the same field for each term; this rule, the JSON shape, and the exit codes are
the contract. How terms are cut, folded, shortened, and ranked may change
(`search.py` describes it; `terms` in the JSON shows what was searched). Case,
width, accents, the dashes U+2010 to U+2015 and U+2212, curly quotes, and
invisible characters do not matter. The fields, best first:

- `title`, without its Markdown markup;
- `lean`, each `lean:` and `mathlib_declaration` name after its last dot, or a
  term that is the whole name or its last components, `_root_.` and `«»` aside;
- `node_id`, the last component of the path ID;
- `statement_text`, as the site publishes it (no headings, code blocks,
  diagrams, comments, or hidden elements), with inline code, and mathematics
  delimited `\(...\)`, so search for a formula's content;
- `ancestors`, the titles of containing articles below the roadmap root;
- `qualified_names`, the path ID and Lean names in full, where a namespace
  alone matches.

Other frontmatter and the sections after the statement are not searched. There
is no phrase, synonym, or prefix search, and one absent term means no hit:
`map` matches `roadmap`, `unambiguous` does not match `non-ambiguous`, and a
symbol such as `ℝ` folds to its plain letter. Only the query is shortened, so
`indices` does not find `index`. Superscript and subscript digits fold to the
digit while `^` and `_` stay, so `S^1` does not find `S¹`, nor `H_0` `H₀`. A
name with a dot, underscore, or digit is searched exactly. Hits sort by the
best field any term matched, and last by node ID.

`--state` (`proved` does not include `fully_proved`) and `--declaration` apply
before `total_matches` is counted, and `--limit` after. `--declaration def`
also keeps `noncomputable def`, but not `definition`. When no article declares
any of the requested kinds, search refuses with exit 2 and names the kinds in
use; a requested kind no article declares, beside one that some article does,
gets a note on standard error.

`--json` writes `autoform-search/v1`: `schema`, `source_revision`,
`open_statements`, `query`, `terms`, `filters` (`declaration` and `state`,
each a list), `limit`, `total_matches`, and `hits`, with no timestamp and no
path outside the project. A hit carries `node_id`, `article_id`,
`article_path`, `article_revision`, `title`, `declaration`, `catalog`
(`module` for a module inventory), `state`, `statement_text` (the authored
Markdown), `lean_targets` (all of them), `mathlib`, `mathlib_declarations`,
`mathlib_file`, `source_targets` (as the article writes them, once each, only
http(s) links and paths inside the blueprint), `used_by` (the first ten
dependents by node ID, through statement or proof), `used_by_count` (all of
them), `shared_title` (advisory: another article repeats the title), and
`matched_fields` (best first, the fields where some term found its best
match); `article_id`, `declaration`, `catalog`, and `mathlib_file` may be null.
A Lean target has `declaration`, `source_file` (relative to `--lean-root`), and
`line`; the last two are null without `--lean-root`, when the lexical scan does
not find the name, or when it finds several with none chosen (see
[Validation](validation-and-audit.md#validation)). No hit is `"hits":[]` with
exit 0. Human output is not a contract.

Search rereads each article it matches against and refuses, with exit 2, bytes
other than those the graph was built from, and a symlinked roadmap entry.

### Searching a library the project depends on

`--library NAME` (repeatable) also searches the index a locked Lake package
publishes, and needs `--lean-root` naming the Lake project. For example,
`autoform search . "separating hyperplane" --lean-root . --library atlas`.
`autoform library list ROOT` says, for each package in `lake-manifest.json`,
whether search would accept it and if not why; its `--json` emits
`autoform-library-list/v1`: `schema` and `packages`, each with `name`, `type`,
`revision`, `index` (whether the checkout holds an index file), `usable`, and
`reason` (`null` when usable). Neither command starts a process or writes a
file. A search without `--library`, in a project that locks a package with an
index file, adds one `note:` line on stderr naming the package and the flags
that search it; the note only checks that the file exists.

The index is the file `autoform-library-index.jsonl` at the package root, or the
file given as `--library NAME=PATH`. Its format, `autoform-library-index/v0`,
may change without notice until it becomes `v1`, and a library hit's fields with
it. Before an index is used, search checks that the checkout is at the locked
revision and pins the index's toolchain and direct dependencies, that the Lean
files under the indexed directories are exactly the indexed modules with the
recorded digests, and that the index is whole. It leaves out, with a
`warning: library NAME:` line on standard error, a package that is not locked,
not checked out, a path dependency, replaced by `.lake/package-overrides.json`,
without an index, or failing a check, and, on POSIX, every library of a project
whose `.lake` or packages directory is a symbolic link. A package replaced with
`lake --packages=` is not detected. Search does not verify the declaration
records, a module's `module_doc` or `module_system`, or the header's
`generator`, `probe`, or `package`, so these can be forged.

A declaration matches when every term occurs in its last name component
(`lean`), docstring, module header (`module_doc`), full name (also without
`_root_.` and `«»`) or module (`qualified_names`), the constants in its type
(`mentions`), or its keyword (`kind`), ranked in that order, then `complete`
before `wanted`, and last by name and module. `--state` and `--declaration`
apply to articles only; `--limit` applies to each list.

With `--library` the JSON schema is `autoform-search/v2`: every
`autoform-search/v1` key keeps its meaning, and `libraries` lists, in the order
given, `name`, `revision`, `index_schema`, `generator`, `probe`, `differences`,
`total_matches`, and `hits`, and `refused` lists each library left out, as
`name` and `reason`. A hit carries the declaration's `name`, `kind`, `status`,
`module`, `source_file`, `line`, `signature`, `statement`, `docstring`,
`mentions`, `auto_named`, `module_system` (`false` for a file a Lean module file
cannot import), `matched_fields`, `module_doc` (the module header when it
matched, otherwise `null`), and `import`, the line that makes a `complete`
declaration available (`null` for a `wanted` one, which carries no proof).
`differences` lists the library's toolchain and each direct dependency whose
locked revision is not the project's, as objects with `what` (`lean_toolchain`
or `dependency`), `name` (`null` for the toolchain), `library` and `project`
(`null` for a side that is a path dependency, and `project` also when the
project lacks it); it does not say the library fails to build. Without
`--library` the output stays `autoform-search/v1`, byte for byte.

#### Publishing an index

`autoform library index ROOT` writes `autoform-library-index.jsonl` at the
package root of a built library, from the modules of every `lean_lib` of the
package, or of those `--lib` names. It imports the library's modules, so run it
on a trusted checkout or in a sandbox. It does not build: a missing or stale
build exits 2 with the `lake build` command for the indexed targets and the
first module Lake found unbuilt. The freshness check, which `--check` also runs,
rewrites Lake's `.hash` files, so `.lake` must be writable. `--timeout` takes a
positive number of seconds (default 600). The index records the Autoform version
that wrote it, so the command refuses to run from an Autoform that is not
installed. The index is written whole or not at all; a failed write says why and
whether the previous index is unchanged.

`library/declarations.py` says which declarations are indexed (those a user
wrote with a declaration keyword), their `line`, and where a `statement` is cut
(in two rare cases, inside the body). One that rests on `sorry` or on an axiom
other than `propext`, `Quot.sound` and `Classical.choice` is left out, with a
count on standard error. A `theorem_wanted`, `proof_wanted`, `def_wanted` or
`instance_wanted` result is indexed with status `wanted`. A range the probe
reports that is not in the source file exits 2 and names the declaration and its
module: the source is not the one the library was built from.

`--check` writes no index. It exits 0 when the index at the root is current,
even if another Autoform version wrote it, 1 when it is stale (as when there is
no index, it is not a regular file, or another probe revision made it), and 2
when it cannot tell: the package root does not exist, the index exists but
cannot be read, or the build is missing or stale. Run it in the library's CI
after the build, with the same `--lib` flags, and pin the Autoform revision.

The generator has been tested on Lean 4.34.1 and 4.35.0 only, CI runs its real
tests on 4.35.0, and it is not supported on Windows. It exits 2, before it runs
Lean, unless the package's `lean-toolchain` is `leanprover/lean4:v4.34.1`,
`leanprover/lean4:v4.35.0`, or a spelling elan resolves to one of them, such as
`v4.35.0`. The index's `package` is the name in the package's
`lake-manifest.json`, else in its `lakefile.toml`, never the folder's; the
command exits 2 when neither gives one. Module names must be ASCII identifiers.
A Lean file under an indexed directory that no indexed library builds is
refused, and so are two modules that declare the same public name in two ways,
which Lean cannot import together. A theorem two modules state the same way is
indexed once, under the first of its modules in name order, with a warning on
standard error naming the modules; each module's copy of a wanted name is
indexed, and a record is identified by its name and module together.

### Citing declarations

`check --lean-root` warns about each declaration of a usable, checked-out
library whose own docstring cites an arXiv paper or DOI that an unproved article
reaches through its `## Sources` links, or through a blueprint Markdown page
such a link names (link text, and pages over 4 MiB, are not read). Identifiers
match in their usual forms: versions; `abs`, `pdf`, `html`, `e-print` and `src`
URLs; old-style ids such as `math.AG/0210149`; arXiv's DOIs; `https://doi.org/`
and `dx.doi.org` links, percent-encoded or not; and the labels `arXiv:`,
`arXiv`, `doi:` and `DOI`, also in backticks, emphasis or a table cell. Silence
it by naming the declaration in full in an implementation note, a page under
`blueprint/sources/` or one a `## Sources` link names, or in project Lean, in
full or by its last name component. Declarations citing a source only through
their module docstring share one warning, silenced by naming the module, or in
full a citing declaration; importing the module names it, importing only an
umbrella module above it does not. A module catalog, or a container whose scope
holds a target and is all proved, reaches nothing; a bare number cites nothing;
and a last-component match counts any identifier, comments and strings included,
so a generic one such as `symm` can hide the warning. Reuse such a declaration
and name it in the note of each article using it; when it does not apply, name
it, with why, on the source's page under `blueprint/sources/`.
