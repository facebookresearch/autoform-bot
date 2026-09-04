# Minimal declaration review

Use this workflow when a person asks which declarations they should inspect to
decide whether a paper, book section, theorem, or other mathematical source was
formalized faithfully. The deliverable is a compact source-to-code checklist,
not an inventory of every declaration in the patch.

## Establish the two exact artifacts

Read the original source itself. A roadmap, issue, PR description, or Lean
docstring is evidence about intent but is not a substitute for the source. If
the source cannot be read, say that source faithfulness cannot yet be judged.

Resolve the exact code revision under review. For a pull request, record both
the base revision and the full head commit SHA. Inspect files and line numbers at
that head SHA rather than assuming the current checkout matches it. Report a
dirty, ahead, behind, or otherwise divergent checkout.

Use the base-to-head diff to distinguish declarations introduced by the change
from pre-existing library declarations. If the user asks for introduced
definitions only, do not silently include pre-existing definitions; identify
them separately as dependencies when necessary.

## Classify before reducing

Classify relevant declarations into four groups:

1. **Semantic definitions** determine what a source object or hypothesis means.
   Include definitions of custom predicates, bundled objects, quotients,
   equivalence relations, and any nonstandard hypothesis used by an endpoint.
2. **Source-facing endpoints** directly express a clause, proposition,
   corollary, or construction in the source.
3. **Bridge declarations** connect the local encoding to a standard library
   notion. Include one only when the endpoint's meaning cannot be checked
   without it.
4. **Implementation helpers** construct maps or discharge proofs without
   changing the public mathematical claim. Exclude these from the first-pass
   checklist.

Do not classify by names or docstrings alone. Read complete signatures and the
definitions on which their mathematical meaning depends. In particular, inspect
typeclass definitions: a strong or hollow custom class can hide the desired
conclusion in a hypothesis.

## Produce the minimal set

For each source clause:

- select the strongest single public endpoint that states it;
- include multiple endpoints only when no one declaration contains all parts;
- include every project-defined notion or hypothesis needed to understand that
  endpoint;
- omit convenience aliases, duplicate wrappers, constructor lemmas, arithmetic
  implementations, and proof-only intermediate results;
- mark a clause **partial** when the endpoint weakens its structure, strengthens
  assumptions, narrows its domain, or omits a conclusion;
- mark a clause **absent** when no introduced declaration states it, even if
  comments claim coverage;
- identify relevant pre-existing declarations without counting them as newly
  introduced work.

After selecting the minimal source-facing endpoints, compute their transitive
statement dependency closure. The roots are the minimal source-facing endpoints,
and an included definition must satisfy both conditions:

1. it was introduced by the pull request; and
2. it is reachable from a root through constants occurring in a declaration's
   type, fields, constructors, or the meaning-bearing body of another included
   definition.

Do not include a definition merely because it is in the same file, namespace,
or pull request. Do not traverse theorem proof bodies: definitions used only to
construct proofs are implementation dependencies, not dependencies of the
statements being reviewed. Record a pre-existing definition separately only
when naming it helps the reviewer understand the closure; never count it as an
introduced definition.

Typical meaning-changing distinctions include an additive equivalence instead
of a linear or ring equivalence, existence of an unspecified isomorphism instead
of a canonical natural isomorphism, an arbitrary presentation instead of the
source's object, and a theorem restricted to `Proj` when the source quantifies
over all projective schemes.

## Generate stable code links

Prefer immutable GitHub blob links:

```text
https://github.com/OWNER/REPO/blob/FULL_COMMIT_SHA/path/to/File.lean#L123
```

Link the declaration name itself and anchor its first declaration line. Do not
use a mutable branch, a pull-request `changes#top` URL, or line numbers from a
different checkout. Group links by source locator, such as section and theorem
number.

If the repository has no accessible web remote, use absolute local file links
and state that they refer to the current checkout rather than an immutable
revision.

## Verify the reviewed snapshot

Build the smallest target containing the selected endpoints from the exact
revision, preferably in a temporary detached worktree so the user's checkout is
untouched. Search the changed Lean files for `sorry`, `admit`, and raw `axiom`.
If the target builds, run `#print axioms` on each source-facing endpoint. If it
does not build, report the first independent failures and do not describe any
declaration as verified or axiom-clean.

Statement review and proof review are separate. A declaration with `sorry` can
still have a faithful signature, but it is not a completed formalization.

## Checklist format

When writing the result to Markdown, use this order:

1. exact source and exact code revision;
2. build and proof-integrity warning, if any;
3. PR-introduced semantic definitions in the endpoints' statement dependency
   closure;
4. source-facing endpoints grouped by source locator;
5. partial or absent source clauses;
6. intentionally omitted implementation helpers and pre-existing dependencies;
7. commands run and unresolved evidence.

When the user supplies a filename, preserve unrelated existing content and add
or update a clearly delimited review-checklist section. Never fabricate a
repository URL, commit SHA, source locator, declaration, or line anchor.
