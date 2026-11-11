# Autoform work

What the `autoform work` reports contain: the frontier and an article's context,
open statements, and the impact of a Lean or import edit. The [CLI
reference](../README.md#work) gives the synopses and exit statuses.

## Work list and context

`work context` takes the path-derived node ID or an `article_id` and reports the
article, dependencies, source and Lean targets, blockers, article and graph
source revisions (the article revision hashes that article and its annotation
alone), and the claim target, which prefers the `article_id`. A Lean target's
`source_file` is relative to `--lean-root`, and null without one or when the
local scan, which skips build output and nested checkouts (any subdirectory with
a `.git` entry), finds the declaration nowhere or several times with none
chosen. Blockers are unmet dependency IDs or `roadmap:not-a-formalizable-leaf`,
`roadmap:missing-article-id`, `roadmap:missing-article-revision`, or
`roadmap:not-ready`. `work list` fails if an unfinished formalizable leaf lacks
an `article_id` (give each one with `autoform links set blueprint ARTICLE`);
`work context` still selects such an article by path ID to report the blocker. An
article recording `statement: retracted` is a revision: JSON `revision` is true,
and the text adds a `revision:` (list) or `Revision:` (context) line saying to
start from `autoform work impact` (see the [revision
contract](claims-and-revisions.md#revision-contract)). Both commands
are read-only and refuse an article whose bytes changed after the graph loaded;
rerun once the project is idle.

Under the open policy the text of `work list` starts with an `Open statements:
allowed` line and adds an `assumes:` line under each item resting on open
statements; `work context` prints `Open statements:` and `Assumes:` lines. JSON
uses `autoform-work/v2`: the frontier and every item carry `open_statements`,
and every item carries `assumes`, the open statements its proof rests on (empty
under the strict policy), and `revision`.

## Work assumptions

`work assumptions` prints the policy, an `open:` line per open statement with
its declarations and the open statements it assumes, a `conditional:` line per
conditional article, and an `unproved:` line per other listed article that
assumes open statements. `--json` writes the `autoform-assumptions/v1` contract
CI audits the build against: every article whose `lean:` names a declaration,
with `open`, `assumes`, and `allowed_open_declarations` (the open declarations
its Lean may reach, plus its own when it is open). A `mathlib: true` article
(state `mathlib`) is never open and is allowed nothing, nor is any article under
the strict policy. Under the open policy, a module catalog that is not proved
keeps only the names no non-catalog article's `lean:` lists, and is left out
when none remain. CI checks that every listed name exists. It
reads Markdown only and needs no Lean build.

## Work impact

`work impact` runs a bounded Lean probe against a fresh build and reports
statement-impacted and proof-impacted articles, unnamed helpers, missing
Markdown dependency paths (`undeclared_dependencies`: impacted articles with no
Markdown path to the revised article, to one naming a revised declaration, or,
when none names it, to an owner), deprecated declarations and their users,
whether the revision is contained (the selected article's target is its only
claim target), and the complete `claim_targets` set. A helper or revised
declaration no article names is claimed under each article naming its nearest
named `parent` ancestor, else the only article naming declarations in its
module, else a stable `lean/<slug>-<digest>` target.
`unused_statement_dependencies`, the stated articles whose Markdown statement
rests on the revised declarations, directly or through other statement
dependencies, but that the probe does not find statement-impacted, join
`claim_targets` too. `mathlib: true` articles are left out. The probe refuses a
loaded root-package module missing from the repository's source inventory, which
skips nested Git checkouts, ignored directories such as `build`, and symlinked
directories, and a module an `M.*` or `M.+` glob finds that it did not load.

The command refuses if the roadmap or the Lean sources change during the probe.
JSON uses `autoform-impact/v1`, bound to the Markdown `source_revision`, the
repository `lean_source_revision`, and a `build_revision` hash of the normalized
Lean records. A missing or stale build is reported as for `work import-impact`.
Changes to notation, attributes, instance priority, or unreported generated
declarations still require human review. Both impact probes import project
modules, so run them only in a trusted checkout or sandbox.

## Work import-impact

`import-impact` is advisory and edits no authored roadmap or Lean source. Its
Lake freshness check may refresh build hash sidecars and, with the artifact
cache `project new` turns on, restore artifacts another checkout built from the
same inputs. By default the probe imports every root library module: a missing
or stale build exits 2 with the `lake build` command, how many modules were
checked, and the first one Lake found unbuilt. `affected_modules`
is `MODULE` and every loaded module that imports it, transitively, complete over
the root libraries' Lake roots and globs. Modules that removing an import drops
from the build are not listed. Like `work impact`, the probe refuses a
root-package module the captured inventory lacks, and one whose loaded source
or artifact is not the captured root one, so a dependency cannot shadow it.
A non-project `MODULE` is refused first, naming the article's module if unique.

Under `--module-scope import-closure` the candidates are `MODULE` and every
root-package module whose source contains each component of a candidate's name,
repeated until none is added. Lean loads the candidate roots and glob modules
and what they import, and stale-build advice names only those. An article owns a
declaration when one of its `lean:` names equals it or is a dotted prefix of it,
so `affected_articles` may list more articles and `article_local` is always
`false`; `affected_unowned_helpers` keep the default scope's rule, and
`unresolved_articles` leaves out a name the source index places outside
`affected_modules` but lists one in a file no `lean_lib` builds. A `.lean` file
that may import a candidate, in a dependency package or uncaptured in a root
library's module tree, makes the command exit 2 and ask for the default scope;
use that scope too when a component of a module name is a common word. An import
naming a candidate only up to case or Unicode normalization can make it exit 2
too, asking for the file's spelling. Either scope's probe, unlike `work impact`,
also refuses a loaded module no `lean_lib` builds, as from a leftover `.olean`.

JSON uses `autoform-module-import-impact/v1`, with `module_graph_revision` and
`module_scope` (`loaded-root-library-graph` or `import-closure`). `article` is
the selected article (`id`, `article_id`, `claim_target`). `claim_targets` holds
only it and `module_claim_target`, `lean-module/<lowercase-slug>-<sha256[:16]>`,
whose key is `module_claim_key`, or only it when `module_generated`: Git
attributes from the index or the clone's `.git/info/attributes`, not the user's
or the system's attribute files, set `linguist-generated` on MODULE's file, or
make it `true`. It is `false` outside a Git work tree or when Git fails.
`affected_claim_targets` are downstream warnings, not ownership requirements;
`claim_keys` and `affected_claim_keys` match the JSON of
`autoform claim list --worker-id WORKER` given those targets.
`affected_articles` lists each article that names, or owns through the parent
chain, a declaration in an affected module, with those `modules`, and
`selected_affected` says whether the selected article is one.
`unresolved_articles` reports local declarations absent from the loaded
environment without blocking unrelated edits, and `affected_unowned_helpers`
carry their own claim targets and keys, as in `work impact`. `article_local`
holds only when `selected_affected` does, `affected_claim_targets` is empty, and
no other article has unresolved declarations.
