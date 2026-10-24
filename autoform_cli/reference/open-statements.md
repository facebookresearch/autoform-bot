# Autoform open statements

## Open statements

By default a project runs the strict policy: CI rejects every `sorry`, so a
theorem's statement lands only together with its proof, and a statement waits
until the proof's prerequisites are proved. `open_statements: allowed` in
`roadmap/README.md` lets a theorem's statement land with a `sorry` proof. Such
an article, a theorem whose statement is formalized and whose proof is not, is
an open statement; CI audits it only through the declarations its `lean:`
names. A proof that uses open statements compiles and records `proof:
formalized`, but is reported as conditionally proved, never as fully proved.
Status, `work`, and the site derive that from the Markdown dependencies and CI
from what the Lean uses, so CI can print `sorry-free` for an article the site
shows as conditional. CI is never the looser of the two: a Lean reach the
Markdown does not declare fails.

State every theorem as a `theorem` with a `sorry` proof, not with
`theorem_wanted` or `proof_wanted`. A dependent can cite the `sorry` proof, but
not a wanted placeholder: `theorem_wanted foo : T` declares a private `def` of
type `ProofWanted T`, not a theorem of `T`. Readiness still counts a
prerequisite that records `statement: formalized` against a wanted target as
stated, so restate or prove such a prerequisite before a dependent cites it.

What is open:

- A retracted theorem (`statement: retracted`) stays open, `sorry` or not,
  until Formalize restates and proves it or the marker and `lean:` are removed.
- A theorem never stated is not open even with `lean:`; CI rejects its `sorry`.
- A definition is never open: CI rejects a `sorry` in it, and its statement
  phase waits until its proof prerequisites are stated. A retracted definition
  is not open, but what rests on it assumes the open statements its body
  reaches.
- A module catalog is never open, even while pending: CI rejects a `sorry` in
  a declaration only it lists and nothing assumes it, though what rests on one
  with `lean:` assumes the open statements its own dependencies reach.

Turn the policy back off only once no open statement remains: the strict audit
rejects every `sorry`, and strict status shows a proof resting on one as
proved, not conditional.

Write an open statement's proof as exactly `sorry`: never in its type, a helper,
a definition or a `where` clause, never inherited from a declaration outside the
root package, and never as one case of a recursive proof, since Lean-generated
auxiliaries count as helpers. `lake build --wfail` and `warningAsError` cannot
be combined with open statements; the generated workflow runs plain `lake
build`.

The generated `autoform-verify.yml` reads the policy with `python3
.github/autoform_audit.py --policy blueprint`, which prints `allowed` or
`forbidden` (`forbidden` when `roadmap/README.md` or the key is absent) and
exits 1 with `error: ...` on malformed frontmatter. Under either policy it
writes the `autoform work assumptions blueprint --json` contract, then:

- Under `forbidden` it runs the strict audit with `--targets`, which rejects
  every `sorry` and refuses a contract that allows open statements, records an
  article as open, or lets one assume an open statement.
- Under `allowed` it audits every root-package declaration against the
  contract. The audit accepts a `sorry` in a declared open statement's own proof
  and a proof that reaches only the open statements its article's Markdown
  dependencies reach. It rejects a `sorry` in a statement or anywhere else in
  the root package, a dependency outside the root package that depends on
  `sorry`, an article declaration that reaches an open statement its Markdown
  dependencies do not (so a fully proved article may reach none), a `lean:`
  name missing from the build, and an open statement its article records as
  proved.

The open-statement audit gives each article declaration at most one status
line, with `NAME` the declaration and `ID` the article's node ID. A declaration
with an error, or one that reaches a failed declaration, gets none, and no
declaration gets one when the kernel replay fails:

```text
open statement (proof is sorry): NAME [ID]
open statement (proof depends on sorry elsewhere): NAME [ID]
open statement (proof is sorry-free; restate it if retracted, then record proof: formalized): NAME [ID]
conditional: NAME [ID] rests on open statement(s) A, B
sorry-free: NAME [ID]
```

It passes with `kernel trust clean except declared open statements (N
root-package declaration(s) audited; K open statement(s), C conditional
declaration(s))`; a failing one logs each error and ends with `root-package
declarations failed the open-statement audit`.

Both audits also require a `lean:` name outside the root package, as a Mathlib
article's is, to be neither unsafe nor partial (a `partial def` counts) and to
use no axiom beyond `propext`, `Classical.choice`, and `Quot.sound`; hold every
root-package declaration to the same three (the open-statement audit also
allows `sorryAx` by the rules above), so `native_decide` fails through
`Lean.trustCompiler`; charge each type and constructor of a mutual inductive
block with the axioms of the whole block; refuse every unsafe or partial
root-package declaration except the partial copy `NAME._unsafe_rec` Lean adds
for each computable recursive definition, which the kernel refuses a safe
declaration to use and the replay skips; and, once every other check has
passed, replay every root-package declaration through the kernel on a fresh
import of the modules outside the root package, logging `kernel replay of the
root package skipped: it runs once every other check passes` until then. The
workflow's scan for the kernel-check bypass option is only lexical. The probe
runs no project code: the workflow runs it with plain `lean`, not `lake env
lean`, with the project's search path in `AUTOFORM_AUDIT_LEAN_PATH` and
`LEAN_ABORT_ON_PANIC=1`.

The audits' errors, each naming what to change:

- `NAME depends on unexpected axiom AX` for a root-package declaration, with
  `AX` such as `Lean.trustCompiler`, or `sorryAx` under the strict audit; the
  strict audit ends with `root-package declarations failed the kernel-trust
  audit`.
- `NAME [ID] is not a declaration of the Lean build; fix the article's lean:
  name or build the module that declares it`: a theorem in a file no target
  imports, or inside a string, passes `autoform check --lean-root` but not this.
- `NAME [ID] is outside the root package and is unsafe or partial`, `NAME [ID]
  is outside the root package and depends on unexpected axiom AX`, and, in the
  open-statement audit, `NAME [ID] is outside the root package and depends on
  sorry`.
- `NAME depends on X, which no module of the build declares`, or for a `lean:`
  name outside the root package `NAME [ID] is outside the root package and
  depends on X, which no module of the build declares`.
- `unsafe or partial declaration: NAME`, with a `partial def` under its own
  name.
- `NAME [ID] is Lean's compiled copy of a recursive definition; name the
  definition itself`, for a `lean:` name that is a `NAME._unsafe_rec` copy.
- `kernel replay of the root package failed: ...`, naming the first
  declaration the kernel rejects.
- `environment already contains 'propext'`: the build declares a core name
  itself, differently from the toolchain.
- `M declares a theorem N in place of the axiom K declares; rename one of them
  so the audit can run`.
- `root module M shares its first component R with the toolchain's library;
  rename the module so the audit can run`, or `root module M shares its first
  component R with another library on the search path (DIRS); rename the module
  so the audit can run`, where DIRS lists the library directories that hold R.
- `AUTOFORM_AUDIT_LEAN_PATH is not set; ...` or `LEAN_PATH is set, ...`: run
  the probe as the local reproduction below does.

The audit does not cover build-time IO: `lake build` runs root and dependency
code and the Lake configuration on the same runner first, and that code can
rewrite the audit script, the toolchain, the workflow's inputs, or the
blueprint. Nor does it defend against a malicious dependency, edits to the
workflow, or crafted `.olean` bytes; closing those needs a separate audit job
that runs no project code, which the generated workflow does not have. The
audit trusts whatever toolchain `lean-toolchain` names: elan, installed with
`--default-toolchain none`, installs any, a fork on GitHub included.

`autoform init` writes `.github/CODEOWNERS.autoform.example`; its comments say
how to activate owner rules for the policy and `.github/`. Owner rules put
changes in front of an owner; they do not stop project code on the CI runner.

A project scaffolded before open statements existed pins `AUTOFORM_REF` to an
Autoform without `work assumptions`, and its workflow runs only the strict
audit. Before opting in, move the pin to a commit that has it and replace
`.github/workflows/autoform-verify.yml` and `.github/autoform_audit.py` together
with what `autoform init` writes there; the new probe refuses an older
workflow's `lake env lean`. With an older pin an opted-in project fails closed:
`autoform check` stops with `unsupported frontmatter key 'open_statements'`.
Under the strict policy the step tolerates argparse's `invalid choice:
'assumptions'`, warning that `AUTOFORM_REF` predates `autoform work assumptions`
and running the strict audit without the `lean:` name check; any other failure,
such as a failed fetch, fails the step.

To reproduce the CI audit locally after a build, with `ROOT_PACKAGE` the Lake
package name the workflow reads from `lake translate-config toml`, and
`--targets` in place of `--open-statements` under the strict policy:

```bash
lake build
lake pack /tmp/autoform-root.tgz
autoform work assumptions blueprint --json > /tmp/autoform-assumptions.json
python3 .github/autoform_audit.py --open-statements /tmp/autoform-assumptions.json \
  ROOT_PACKAGE /tmp/autoform-root.tgz /tmp/autoform-probe.lean
AUTOFORM_AUDIT_LEAN_PATH="$(lake env printenv LEAN_PATH)" LEAN_ABORT_ON_PANIC=1 \
  lean /tmp/autoform-probe.lean
```

The audit passes only when `lean` exits 0 and the last line it prints is the
`kernel trust clean ...` line; the workflow fails the step otherwise.
