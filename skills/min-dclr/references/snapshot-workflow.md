# Snapshot declaration review

Produce the smallest current list a person must inspect to decide whether a
paper, book section, theorem, or other mathematical source is represented by
the Lean statements. This is a refreshable source-to-code checklist, not a full
inventory or a rubric-based audit.

## Capture one coherent snapshot

Read the original mathematical source. A roadmap, issue, pull-request
description, or docstring is evidence about intent but is not a substitute for
the source. If it is unavailable, report that statement faithfulness cannot be
judged.

For each source-facing root, if the source text is available, the managed
section **must** include the original mathematical statement or definition as a
verbatim quotation with its exact source locator, paired in the same checklist
entry with the GitHub link to the Lean declaration that implements it. Link to
the Lean code; do not copy that code into the Markdown. This applies on every
refresh. If the source text is unavailable, state that explicitly and do not
reconstruct a quotation from comments or memory. A clearly labeled paraphrase
may be added when it is grounded in an available secondary source.

At the start of every invocation, record:

- the repository and branch;
- the current commit SHA and intended comparison base;
- staged, unstaged, and untracked files relevant to the review; and
- the source revision or locator.

Do not wait for all formalization runs to finish. The current checkout is the
requested snapshot. Before writing the result, confirm that its commit, status,
and reviewed files have not changed. If they changed during the review, discard
the computed list and rerun once from the new snapshot. If the repository keeps
changing, stop and report that no coherent snapshot could be captured.

Use the base-to-snapshot diff to distinguish introduced declarations from
pre-existing library declarations. A dirty checkout is reviewable, but label it
as uncommitted and do not pretend it has immutable GitHub links.

## Select statements and definitions

For each source clause, select the strongest single public Lean declaration
that states it. Use multiple roots only when no single declaration contains all
parts. Mark a clause **partial** when the Lean statement weakens its structure,
strengthens assumptions, narrows its domain, or omits a conclusion. Mark it
**absent** when no declaration in the snapshot states it.

The selected source-facing declarations are the roots. Compute their transitive
statement dependency closure with the deterministic CLI, repeating `--module`
and `--root` as needed:

```bash
autoform declaration-closure --lean-root . --base BASE_SHA \
  --module Project.Entry --root Project.result --json
```

The CLI builds and imports the requested modules, reads Lean's elaborated
constant expressions, traverses root types and reachable definition values,
and intersects the result with declarations added or changed relative to the
Git base. Its JSON result is the sole authority for closure membership. Do not
add or remove closure entries based on an LLM reading of identifiers.

Include a definition only when both are true:

1. the definition was introduced or materially changed between the comparison
   base and the current snapshot; and
2. the CLI reports it reachable from a root through elaborated constants in a
   declaration's type, fields, constructors, or the value of another included
   definition.

The CLI traverses complete signatures and definition values. A custom class or
predicate can hide the conclusion being reviewed. It traverses theorem types
but never theorem proof values, so proof-only dependencies do not enter the
closure.

For every included structure, class, or inductive declaration, inspect every
field or constructor type and add introduced definitions referenced there,
including references nested beneath `∀`, `∃`, function arrows, typeclass
arguments, and other binders. Repeat this expansion for each newly included
definition until no new statement dependency is found. Do not stop at a public
wrapper when its witness or coherence data is supplied by another introduced
predicate; that predicate is part of the wrapper's mathematical data.

Exclude unrelated definitions from the same file or change and declarations
used only by theorem proof values. Do not manually exclude a reported
definition because it looks like a convenience alias, private helper,
arithmetic implementation, or representation detail: if the CLI reports it,
it is in the requested transitive closure. Name a pre-existing dependency
separately only when it is needed to understand an included declaration; never
count it as an introduced definition.

If `lake build` or Lean elaboration fails, the CLI exits unsuccessfully and no
exact closure exists for that snapshot. Record the failure and omit the closure
list; never substitute a lexical or LLM-generated approximation.

## Link and verify the snapshot

For a clean committed snapshot, link declaration names to immutable GitHub blob
URLs at the full commit SHA:

```text
https://github.com/OWNER/REPO/blob/FULL_COMMIT_SHA/path/to/File.lean#L123
```

For an uncommitted snapshot, use absolute local file links and label them as
local and mutable. On the next invocation after a commit, replace them with
commit-pinned links. Never retain an old link merely because its declaration
name still exists.

Before writing, validate every generated link against the captured snapshot:
the commit must match, the path must exist at that commit, and the anchor must
be the declaration's first line. Reusing an old checklist's line number without
checking it is not validation.

Build the smallest target containing the selected roots. Search changed Lean
files for `sorry`, `admit`, and raw `axiom`. If the target builds, run
`#print axioms` on each source-facing endpoint. Report failures without calling
the declarations verified or axiom-clean.

## Replace the managed list

Recompute the output from scratch on every invocation. In the target Markdown
file, replace everything between these markers:

```markdown
<!-- min-dclr:start -->
<!-- min-dclr:end -->
```

Create the marked section if it does not exist. If either marker is duplicated
or only one marker exists, stop instead of guessing which content to replace.
Never incrementally append to the old list. Replacement must delete stale
entries and add current ones while preserving all unrelated content.

When an unmarked target consists entirely of an older declaration checklist,
migrate it by replacing that checklist with one marked section. When the file
also has unrelated content, preserve that content and insert the new marked
section separately.

Write the managed section in this order:

1. source locator and exact snapshot, including dirty-state disclosure;
2. build and proof-integrity warning, if any;
3. introduced definitions in the roots' statement dependency closure;
4. source-facing declarations grouped by source locator, with each available
   original mathematical statement or definition quoted verbatim and paired
   with its implementing Lean declaration's GitHub link;
5. partial or absent source clauses;
6. omitted implementation helpers and named pre-existing dependencies; and
7. commands run and unresolved evidence.

Never fabricate a source locator, declaration, repository URL, commit SHA, or
line anchor.
