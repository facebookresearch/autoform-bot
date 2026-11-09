# Blueprint format and CLI

The Autoform CLI validates, visualizes, and publishes the multilevel dependency
graph in `blueprint/roadmap/`. The Markdown book is the graph.

## Articles and containment

See [Blueprint format](reference/blueprint-format.md).

## Assertions and derived status

See [Blueprint format](reference/blueprint-format.md#assertions-and-derived-status).

## Commands

This section gives the main commands' synopses and [Contracts](#contracts) the
rest; skills link to both, and every invocation in all three is parse-checked.

Commands are written as they appear on `PATH` with this package installed; in a
consumer project, where it is not, run each from the project root with the
prefix `uv run --project "<AUTOFORM_PLUGIN_ROOT>"`, the root resolved from the
loaded plugin, as [Publishing](#publishing-and-the-dashboard) shows.

`autoform --version` prints the installed version, followed by the plugin
checkout's commit when it runs from one, as in `autoform 0.6.0 (745a88599469)`.

### init

Create a new project's vault, site configuration, and CI. Existing files are
left alone, so the same command repairs a project. `--force` instead overwrites
every file `init` manages, `blueprint/roadmap/README.md`,
`blueprint/coverage/README.md`, `README.md` and `mkdocs.yml` included, except a
turned-off verify workflow, and replaces a root `.gitignore`:

```bash
autoform init . --title "Finite Flat Group Schemes" \
  --repository-url https://github.com/owner/repo
```

[Project setup](reference/project-setup.md#init) gives the `.gitignore` rules,
refused names, `--no-verify-workflow`, and the pin without which CI is omitted.

### project

Create or inspect a Lean project, report its worktrees, and list Autoform's
bundled release pairs, the ones `project new` can lock offline:

```bash
autoform project versions
autoform project new ./FiniteFlat --package FiniteFlat
autoform project new ./FiniteFlat430 --package FiniteFlat --lean-toolchain v4.30.0
autoform project new ./Gauss --package Gauss --library atlas
autoform project inspect path/inside/project --json
```

`project new` builds a complete Lean shell (the release's resolved dependency
manifest, blueprint, and site) in a private sibling directory and publishes it
atomically: the target must not exist and is never overwritten, and a failed or
concurrent creation leaves no partial target. Its errors name a code, which
[Project setup](reference/project-setup.md#project-new) lists.

`project inspect` is read-only and offline. Automation should use `--json`,
accept only `autoform-project-inspection/v1`, and branch on `ok`,
`compatibility.status`, and `diagnostics[].code`, not message text. The
[project-inspection reference](project/README.md) defines the statuses, exit
codes, JSON fields, and diagnostic codes.

`autoform project worktrees --json` lists each linked worktree with the codes
that protect it, and `--remove MANIFEST` removes the unprotected ones after
checking each again ([Project setup](reference/project-setup.md#project-worktrees)).
Exit 1: no checkout, bad manifest, or a refusal.

### Publishing and the dashboard

Publishing a project runs four steps in order:

```bash
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform check blueprint --lean-root .
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform-visualize blueprint
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform render blueprint \
  --output site-src --lean-root . --require-declarations
uv run --with mkdocs --with mkdocs-material --with mkdocs-literate-nav \
  --with pymdown-extensions mkdocs build --strict
```

Drop `--require-declarations` when reviewing work in progress, where a
statement may name a Lean declaration that does not exist yet.

`autoform dashboard . --site-dir site` then serves the built site on `127.0.0.1`
until you stop it, with a read-only live overlay of current author claims. The
generated `blueprint-pages.yml` deploys only a commit that `autoform-verify.yml`
passed on a push to `main` (see [the published site](reference/site.md)).

### check

Validate structure and, with `--lean-root`, that every `lean:` name exists:

```bash
autoform check blueprint --lean-root .
```

`check` resolves names only in the project's own Lean files, not in Mathlib or
other dependencies under `.lake`. A name not found, unless the error gives its
type's `path:line` ([Validation](reference/validation-and-audit.md#validation)),
adds a note at the end of the output: a shared library's declaration belongs in
the article's implementation note. It also warns about a shared library's
declaration that [cites a source](reference/search.md#citing-declarations) an
unproved article reaches, a library index it cannot read or parse, a library
with such a declaration that fails verification against its checkout, and Lake
files it cannot use. None changes the exit status.

`--no-site-warnings` leaves the site's page warnings to a `render` that runs
next ([Validation](reference/validation-and-audit.md#validation)); the generated
`blueprint-pages.yml` passes it, and `autoform-verify.yml` does not.

### audit and coverage

```bash
autoform audit blueprint --lean-root .
```

`audit` adds deterministic completeness, provenance,
[coverage](reference/blueprint-format.md#coverage), checked-fact,
and optional Lean-target checks ([Audit contract](reference/validation-and-audit.md#audit-contract)).
It is read-only and offline. Pass `--json` for stable machine-readable output.
A nonzero exit status means at least one finding;
[warnings](reference/validation-and-audit.md#warnings) never change it.

### skeleton

Extract what a reader must trust for each formalized statement:

```bash
autoform skeleton blueprint --lean-root .
autoform skeleton blueprint --lean-root . --node chapter/main-result
autoform skeleton blueprint --lean-root . --output skeleton.json --packets review-packets --passages review-passages
```

The skeleton of a `lean:` declaration is its elaborated signature, then every
project declaration its *statement* rests on, quoted from the sources: a
definition with its body, a theorem with its type only. It reports the axioms
the declaration rests on (a `sorry` shows as `sorryAx`). `--output` records the
`autoform-skeleton/v5` report, `--packets DIR` writes one packet per skeleton,
and `--passages DIR`, which requires `--packets`, each article's cited passage.

Run skeleton extraction only in a trusted checkout or an operating-system
sandbox: Lake evaluates `lakefile.lean`, and the probe imports project code
that may perform arbitrary IO and can forge probe output. `--timeout SECONDS`
sets the helper's and each probe's budget, 600 seconds by default.

[Skeletons and statement review](reference/skeleton-and-review.md) gives what a
skeleton holds, the resolution rules, the report, packets and passages, what
each hash binds, and what a read-back card holds and when it is refused.

### review

Review a formalized statement with three commands, then publish the review:

```bash
autoform review prepare blueprint --lean-root . --packets review-packets
autoform review record blueprint --packets review-packets \
  --article-id af_0123456789abcdef01234567 --declaration Ns.result \
  --testimony read-back.txt --model muse-spark-1.1
autoform review check blueprint --lean-root .
autoform render blueprint --lean-root . --review
```

`review prepare` writes one blind packet per declaration of every Lean-mapped
article. `review record` runs no Lean and files a blind auditor's read-back as a
card. `review check` fails until every Lean-mapped article has current cards and
a `review_approved` equal to the review hash it prints. `render --review` puts a
*Review* disclosure under every formalized statement. [Statement
review](reference/skeleton-and-review.md#statement-review) gives each command's
rules and refusals. `review prepare`, `review check`, and `render --review` take
the same `--timeout SECONDS` as `skeleton`.

`review prepare` and `review record` exit 2 on input they cannot derive, read,
or trust, on refused testimony, and on a card conflict. `review check` exits 1
on findings, and 2 on evidence it cannot derive or a blueprint that changed
during the run. `render --review` exits 1 on a refused review, printing its
reasons on stdout; `--timeout` is accepted only with `--review`.

The gate is opt-in: commit `blueprint/.autoform-review` holding
`autoform-review-policy/v1` and a line end; `autoform init` never writes it.
With the marker, generated CI runs `review check` after the Lean build, without
making advisory roadmap or coverage findings merge blockers, and deleting every
card cannot turn it off. Without it, cards or `review_approved` fail CI.

`review_approved` is a drift checksum, not an authorization: anyone, an agent
included, can paste the hash `review check` prints, and a current approval is
labelled self-approved wherever it is shown. Authorization needs a CODEOWNERS
rule for `/blueprint/` (in `CODEOWNERS.autoform.example`, activated as under
[Open statements](reference/open-statements.md#open-statements)) and a
default-branch ruleset that requires code-owner review, dismisses stale
approvals, and requires approval of the latest push.

### work

Inspect the current Markdown-derived formalization frontier, and list the open
statements and the articles that rest on them:

```bash
autoform work list .
autoform work list . --json
autoform work context chapter/result . --json
autoform work assumptions .
autoform work assumptions blueprint --json
```

`work list` returns the formalizable leaves whose next statement or proof phase
is unblocked, by the rule behind the derived `can_state` and `can_prove` states
and the site's Next up card (see [Open
statements](reference/open-statements.md#open-statements)).

`work context` takes the path-derived node ID or an `article_id`. [Work list and
context](reference/work.md#work-list-and-context) gives the report, the
`autoform-work/v2` JSON, blockers, revision lines and open-policy output.

`work assumptions --json` writes the `autoform-assumptions/v1` contract CI
audits the build against ([work assumptions](reference/work.md#work-assumptions)).

#### work impact

Before editing an article's Lean declarations, ask what revising them affects:

```bash
autoform work impact chapter/result . --lean-root .
autoform work impact chapter/result . --lean-root . --declaration MyProject.helper --json
```

`work impact` runs a bounded Lean probe against a fresh build ([work
impact](reference/work.md#work-impact)). Both impact commands exit 2 on any
refusal, a missing or stale build included, and import project modules, so run
them only in a trusted checkout or sandbox.

#### work import-impact

Report an import edit's coordination and review scope without forbidding it:

```bash
autoform work import-impact SELECTOR MODULE [TARGET] --lean-root PATH [--json] [--timeout SECONDS] [--module-scope SCOPE]
```

`import-impact` is advisory and edits no authored roadmap or Lean source. By
default the probe imports every root library module, so each must be built here
or, with Lake's artifact cache on, from the same inputs elsewhere; a missing or
stale build exits 2. [work import-impact](reference/work.md#work-import-impact)
gives the scopes, ownership rules, JSON and every field.

Use the [Formalize import workflow](../skills/formalize/SKILL.md#import-edits).

### search

Before adding an article or a Lean helper, check the blueprint for its result:

```bash
autoform search . "separating hyperplane" --lean-root .
autoform search . "non-ambiguous" --lean-root . --json --limit 20
autoform search blueprint "interlacing"
autoform library list .
```

`search` takes the project or blueprint directory, then one quoted query; both
are required. A query that starts with `-` goes last, after `--`. It exits 0
with or without hits ([search contract](reference/search.md#search-contract)).

### migrate

Plan durable article identity metadata, and add it:

```bash
autoform migrate article-ids blueprint --json
autoform migrate article-ids blueprint --check
autoform migrate article-ids blueprint --write
```

The planner is read-only without `--write`, proposing deterministic IDs for
articles that lack one; `--write` adds each as the first frontmatter line after
checking every article, refusing one that changed since the plan or became a
symlink, and a rerun completes the plan. [`migrate links`](reference/links.md)
takes the same flags; this version's skills still write frontmatter, so a
project agents work in waits for the release whose skills write annotations.

### claim

Coordinate temporary cross-machine ownership without modifying the book:

```bash
export AUTOFORM_WORKER_ID="agent-name"
autoform claim acquire af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
autoform claim release af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
autoform claim list af_5b0e4d3c2a1f09e8d7c6b5a4 af_0123456789abcdef01234567
```

Claims are optional: use them when the user asks, or when another session on the
project already does; a single Formalize run needs none. A claim excludes only
workers that also claim. Claim an article by the `claim_target` that `work
context` reports: claims on its path ID and on its `article_id` do not exclude
each other. Each concurrent agent needs its own worker ID, since a second
acquire by the same owner succeeds; where shell state does not persist, pass
`--worker-id` on every command. A claim lasts seven days; just before
integrating, re-run `acquire` on the whole set with the same `--note`, and stop
if it is refused. Acquire a whole set in one command (see the [claim
contract](reference/claims-and-revisions.md#claim-contract), which also covers
`acquire --note`).

Claims are fail-closed compare-and-swap leases under `refs/autoform-claims/` on
the Git `origin`; pass `--repo` for another board. A failed acquire means the
caller cannot prove ownership and must stop before committing or pushing
protected work. Claims prove no mathematical correctness and do not replace
branch-level Git CAS.

### debrief

Ask the agent that led a Setup, Roadmap, or Formalize run what got in its way,
and draft an AutoformBot issue from the answer:

```bash
autoform debrief orchestrator form . --skill formalize
autoform debrief orchestrator record . --skill formalize < /tmp/answer.json
```

Ask the agent that just finished a formalization attempt what infrastructure
would have made it cheaper, and keep its answer:

```bash
autoform debrief subagent form af_5b0e4d3c2a1f09e8d7c6b5a4 . --phase proof
autoform debrief subagent record af_5b0e4d3c2a1f09e8d7c6b5a4 . --phase proof \
  --worker-id agent-name < /tmp/answer.json
```

Orchestrator debriefs are on unless `AUTOFORM_DEBRIEF` turns them off; subagent
debriefs need `AUTOFORM_SUBAGENT_DEBRIEF=1`. Both share one store that Git does
not track, which `AUTOFORM_DEBRIEF_DIR` moves; [Debriefs](reference/debrief.md)
gives the form, what `record` stores and prints, and where the store lives.

### visualize and render

Refresh the vault's dependency summary, `blueprint/dependencies.md`:

```bash
autoform-visualize blueprint
```

The summary is plain Markdown built from the explorer's project map (the
scaffolded vault ignores it): the statuses, one row per chapter with its items,
ready work and dependencies both ways, the articles while the roadmap has at
most 120, and how to open the explorer. Its front matter carries `kind: graph`
and `autoform_generated: true`. It replaces a missing page or one Autoform
wrote, and exits 2 refusing any other page.

Build the site source, with links to Lean declarations at the current commit:

```bash
autoform render blueprint --output site-src --lean-root . --require-declarations
```

`render` never writes into the vault. Once the roadmap has articles, the landing
page combines the mathematical reader and dependency map in one full-page
client. [The published site](reference/site.md#rendered-pages) gives the rest,
[MathJax](reference/site.md#mathjax) included.

`--ileans DIR` names a Lean build's `lib/lean` directory, such as
`.lake/build/lib/lean`. Each name in a statement's Lean then links the
declaration Lean resolved it to: a project's as above, and a constant of a
package Mathlib's documentation covers to its page there. A file without usable
`.ilean` data, or a declaration that data shows to be older than the source,
links as without the option, with a warning. An edit the data cannot show, such
as to an `open` line, is not caught, so build first; Pages passes the verified
commit's own build ([Pages deploys](reference/site.md#pages-deploys)).

## Contracts

Each area's contract has its own page:

- [Blueprint format](reference/blueprint-format.md): articles, frontmatter assertions, derived status, and the coverage contract.
- [Validation and audit](reference/validation-and-audit.md): what `check` and `audit` refuse or warn about.
- [Search](reference/search.md): `autoform search`, searching a library the project depends on, publishing a library's index, and a library declaration that cites a source.
- [Pinned name lists](reference/pinned-names.md): a library's names at a pinned revision, and what `check` warns about them.
- [Annotations](reference/links.md): the literature-links annotations in `blueprint/links/` that carry articles' facts.
- [Skeletons and statement review](reference/skeleton-and-review.md): what `skeleton` extracts, its hashes, read-back cards, and the `review` rules.
- [Open statements](reference/open-statements.md): conditional results and the open-statement policy.
- [Claims and revisions](reference/claims-and-revisions.md): leases and revising a published statement.
- [Work](reference/work.md): the frontier, open statements, and the impact of a Lean or import edit.
- [Debriefs](reference/debrief.md): what the debrief commands ask, store and print.
- [Copied code](reference/abstraction.md): how `autoform abstraction` clusters copies and records decisions.
- [Project setup](reference/project-setup.md): what `init` and `project` write, refuse and report.
- [The published site](reference/site.md): what render writes and publishes, MathJax, subdirectory projects, the dashboard overlay, and Pages deploys and reports.
- [Runtime and publication](reference/runtime-and-publication.md): `doctor`, the in-memory runtime projection and the published site.
- [Upgrading](../CHANGELOG.md#upgrading-from-050): what a repository meets on a new version, including what CI at an older `AUTOFORM_REF` rejects.
