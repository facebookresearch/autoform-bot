# Testing the Prove2Me integration

How to exercise `autoform prove2me`, the `prove2me` skill, and
`formalize_statement.sh`. The stages run from safest to riskiest; finish each
before moving on. Only stages 4 and 7 write to Prove2Me, and stage 4 keeps
everything private.

All commands run from the root of this checkout unless stated otherwise.

## Prerequisites

- `git`, `uv`, `lake` (via [elan](https://github.com/leanprover/elan)), and
  `claude` on `PATH`.
- A Prove2Me account. New accounts register at <https://prove2.me>; see
  <https://prove2.me/start.md>. Stage 0 clones the workspace and asks for the
  API key, so neither has to exist beforehand.
- Free disk space: each Mathlib environment needs its own checkout of about
  7-15 GB.

Throughout, the example mission is
[Positive definite matrix integral inequality](https://prove2.me/missions/Positive_definite_matrix_integral_inequality);
its open leaf at the time of writing is
`RybinAI2026.P01.one_sphere_l2_coefficients`
(`60f0b1c6-208b-4532-a25d-92cf073d331e`), verified in Mathlib `c5ea003`
(Lean v4.30.0).

## 0. Set up the workspace

One command clones the workspace, stores the key, and prepares the mission's
Mathlib, each only if missing, so it is safe to rerun:

```bash
uv run autoform prove2me setup --mission Positive_definite_matrix_integral_inequality
```

1. **Clone** <https://github.com/prove2me/prove2me_workspace> into
   `~/prove2me_workspace` (or `$PROVE2ME_WORKSPACE`).
2. **Key.** Prompts with hidden input (mint one under the account menu >
   API key), checks it, and saves it to that workspace's `credentials.json`.
   Rerun `setup` to replace an expired key.
3. **Mathlib.** Uses the default workspace if it is unpinned or already on
   the mission's environment, otherwise a sibling `prove2me-<rev7>` whose
   `credentials.json` links back to the key. It writes the gitignored
   `lean-toolchain` and `lakefile.lean`, then fetches the Mathlib cache and
   builds the smoke test. An existing pin is never changed.
4. **Confirm.** It asks before cloning or downloading, and refuses the
   download when less than about 16 GB is free.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--env <rev>` or `--env default` | Pin to an environment directly instead of a mission's |
| `--no-fetch` | Clone and pin, but skip the Mathlib download |
| `--yes` | Skip the confirmation, for unattended use |
| `--print-workspace` | Print only the chosen checkout's path, for scripts |
| `--json` | Report the workspace, its pin, and whether Mathlib is ready |

To point the rest of this guide at the checkout it chose:

```bash
export PROVE2ME_WORKSPACE="$(uv run autoform prove2me setup \
  --mission Positive_definite_matrix_integral_inequality --print-workspace)"
```

`formalize_statement.sh` runs `setup` itself. Missions use different
environments (Weak Goldbach is on `0df444a`, the matrix inequality mission on
`c5ea003`), and each new one is another multi-gigabyte checkout.

## 1. Unit tests (offline)

```bash
uv run pytest tests/test_prove2me.py -q
```

These use an in-process fake of the platform: no network, no key. They cover
the key lookup, the host restriction, mirroring, the local gates, polling, and
the `verify`, `publish`, `propose`, and `setup` commands. The plugin checks:

```bash
uv run pytest tests/test_skill_examples.py tests/test_plugin_runtime.py tests/test_cli.py -q
uv run ruff check autoform_cli servers tests
```

The full suite (`make test`) runs real Lean builds and takes over half an
hour; skip them while iterating with `--ignore=tests/test_skeleton.py`. Four
`tests/test_scaffold.py` tests fail when `origin` is an SSH alias
(`org-…@github.com:…`), independently of Prove2Me.

## 2. Read-only commands against the live platform

```bash
uv run autoform prove2me auth
uv run autoform prove2me mission Positive_definite_matrix_integral_inequality
uv run autoform prove2me get /theorems/60f0b1c6-208b-4532-a25d-92cf073d331e/submissions
```

Expect `auth` to print your username, the key's expiry, and the platform
version, and never the key itself. `mission` also accepts the full URL, the
plain name, or the mission id. `get` refuses anything that is not a path under
the API, for example `https://example.com/x`.

`fetch` writes files, so point it at a scratch workspace first:

```bash
mkdir -p /tmp/p2m-scratch
cp ~/prove2me_workspace/{lakefile.lean,lake-manifest.json} /tmp/p2m-scratch/
uv run autoform prove2me fetch 60f0b1c6-208b-4532-a25d-92cf073d331e --workspace /tmp/p2m-scratch
```

Expect `Theorems/Thm_RybinAI2026_P01_one_sphere_l2_coefficients.lean` and two
`Definitions/Def_rybin2026_p01_*.lean` files (the second is imported by the
first), plus a warning if the workspace pins a different Mathlib. Running it
again should report every file as unchanged (`=`).

## 3. Local gates without submitting

`verify --dry-run` never writes to the platform. With `--skip-compile` it runs
only the source checks, so any workspace will do:

```bash
cd /tmp/p2m-scratch && mkdir -p Solutions
printf 'namespace X\ntheorem solution : True := by sorry\nend X\n' > Solutions/bad.lean
printf 'theorem solution : True := trivial\n' > Solutions/good.lean
cd -
for f in bad good; do
  uv run autoform prove2me verify 60f0b1c6-208b-4532-a25d-92cf073d331e \
    /tmp/p2m-scratch/Solutions/$f.lean --workspace /tmp/p2m-scratch --dry-run --skip-compile
done
```

Expect `bad.lean` to fail on `sorry` and on the namespace, and `good.lean` to
pass. Passing here says nothing about the statement: that is the compile step.

### The full dry run needs a matching workspace

Without `--skip-compile`, `verify` refuses a Mathlib mismatch, re-mirrors the
target, builds the solution, and checks
`example : type_of% @<target> := @solution`. For this mission that needs the
`c5ea003` checkout from stage 0 as `$PROVE2ME_WORKSPACE`. To confirm the pin
resolved, `git -C "$PROVE2ME_WORKSPACE/.lake/packages/mathlib" rev-parse HEAD`
must print `c5ea00351c28e24afc9f0f84379aa41082b1188f`. If
`lake build Solutions.SmokeTest` ever starts compiling Mathlib modules one by
one, the cache missed: stop it and rerun `lake exe cache get` in the checkout.

Then put solutions under `$PROVE2ME_WORKSPACE/Solutions/` and confirm each
outcome:

| File | Expected |
| --- | --- |
| A correct proof | `OK: … passes the local gates` |
| A proof of a different statement | `Type mismatch` and "does not match the target's formal_statement" |
| Contains `sorry` | Refused before compiling |
| Outside `Solutions/` | Refused: the type check needs an importable module |

The type check compares types up to definitional equality and ignores binder
annotations, so `{n : ℕ}` versus `(n : ℕ)` passes locally even though the
platform asks for identical binders. The server's verdict is final.

## 4. A real submission, kept private

This is the only way to exercise `publish`, `verify` without `--dry-run`,
verdict polling, and `explain` end to end. A private theorem is visible only
to you, but it still occupies its name in its environment, so pick a unique
one.

Create `smoke.json`:

```json
{
  "theorem_name": "chaikon_autoform_smoke_1",
  "theorem_title": "Autoform smoke test",
  "formal_statement": "theorem chaikon_autoform_smoke_1 : True := by sorry",
  "natural_language_statement": "Truth holds; a private test of the submission pipeline."
}
```

```bash
uv run autoform prove2me publish problem smoke.json --env default --private --dry-run
uv run autoform prove2me publish problem smoke.json --env default --private
# prints: chaikon_autoform_smoke_1: PUBLISHED job … -> theorem <new-id>

echo 'theorem solution : True := trivial' > ~/prove2me_workspace/Solutions/Sol_smoke.lean
uv run autoform prove2me verify <new-id> ~/prove2me_workspace/Solutions/Sol_smoke.lean \
  --workspace ~/prove2me_workspace --allow-env-mismatch
# expect: status: ACCEPTED

echo 'The statement is `True`, closed by `trivial`.' > why.md
uv run autoform prove2me explain <submission-id> --file why.md
```

`--allow-env-mismatch` is safe here only because the file imports nothing
from Mathlib, so the local build and type check are still meaningful. Do not
use it for real targets.

Also worth trying on the private theorem: a file that proves the wrong
statement with `--skip-compile` (expect `WA` from the server), and `--json`
on each command.

Afterwards, retire the theorem with Prove2Me's deprecation flag
(`references/contribute.md`); the CLI does not wrap that yet.

## 5. The skill, interactively

Start Claude Code in the Prove2Me workspace with this checkout as the plugin:

```bash
cd ~/prove2me_workspace
claude --plugin-dir /path/to/autoform-bot
```

If autoform is also installed from the marketplace, disable that copy first
(`/plugin`); it has no `prove2me` skill and the two would collide.

Then ask, for example:

> /autoform:prove2me pick a theorem in Positive_definite_matrix_integral_inequality and show me a dry run

You approve each tool call, so check that it:

- reads the workspace's `SKILL.md`, `references/mission_solver.md`, and
  `references/prove.md` before acting;
- uses `autoform prove2me …` for every API call, and never `curl`, a
  hand-written HTTP client, or `credentials.json`;
- scouts milestone history, prior submissions, and discussion before choosing;
- notices the Mathlib mismatch instead of compiling against the wrong one;
- follows the cited source rather than improvising a proof;
- stops at `verify --dry-run` and asks before submitting;
- writes an explanation that reads like a section of a paper.

Also check it stays out of the way: in a fresh session, "build a roadmap for
this paper" should use the roadmap skill, not this one.

Skills load when a session starts, so restart Claude Code after editing
`skills/prove2me/SKILL.md`.

## 6. The script, without spending a Claude run

`formalize_statement.sh` only assembles a prompt and a permission list, then
calls `claude`. A stand-in `claude` that prints its arguments shows exactly
what would run:

```bash
mkdir -p /tmp/fakebin
printf '#!/bin/bash\nprintf "ARG: %%s\\n" "$@"\n' > /tmp/fakebin/claude
chmod +x /tmp/fakebin/claude

PATH=/tmp/fakebin:$PATH ./formalize_statement.sh --prove2me Positive_definite_matrix_integral_inequality --dry-run
PATH=/tmp/fakebin:$PATH PROVE2ME_API_KEY=p2m_x.eyJleHAiOjEwMDB9.x ./formalize_statement.sh --prove2me x
```

Expect the first to authenticate for real, then print the prompt and an
allow-list without `curl` or `python3`. Expect the second to stop with an
expired-key error before reaching `claude`. The `git pull` of the workspace
may wait on the network; it only warns on failure.

Statement mode works the same way, but its first real run creates
`~/autoform_statements` (or `$AUTOFORM_STATEMENTS_DIR`, which must be outside
this checkout) and downloads Mathlib; use `--roadmap-only` or `--project` to
avoid that.

## 7. The script, for real

```bash
./formalize_statement.sh --prove2me Positive_definite_matrix_integral_inequality --dry-run
./formalize_statement.sh "6 is not a prime number"
./formalize_statement.sh --propose --dry-run "6 is not a prime number"
```

The first runs the skill unattended with a fixed permission list. Anything it
needed but was not allowed shows up as a refused tool call in its final
report. Drop `--dry-run` only once stages 4 and 5 look right: that run
publishes under your account and the platform's Apache 2.0 terms.

The third also drafts a mission proposal in the platform's default Mathlib,
choosing the type and fields itself (`--mission-type` overrides), and stops
after `propose --dry-run` with the spec at `prove2me/proposal.json`. Without
`--dry-run` it uploads a private draft; nothing is published until you
confirm each item under **My missions** on prove2.me and click **Submit
Proposal**.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `expired at …` before any request | The key is past its `exp`; mint a new one and rerun `autoform prove2me setup` to enter it. |
| `rejected the API key … expired or been revoked` | The server refused the key; mint a new one and rerun `setup`. |
| `not asking without a terminal; … pass --yes` | `setup` needed to clone or download but had no terminal to ask in; run it yourself first. |
| `CERTIFICATE_VERIFY_FAILED` from other Python tools | The system Python lacks a CA store. The CLI uses httpx with certifi and is unaffected. |
| `workspace pins Mathlib … re-pin` | The target lives in another environment; use a matching checkout (stage 3). |
| `no configuration file with a supported extension` | A fresh workspace clone has no `lakefile.lean`; run `autoform prove2me setup --mission …` (stage 0). |
| `only … GB is free (needs about 16 GB)` | Setup will not start a Mathlib download that cannot fit. Free space, or reuse a checkout already on that environment. |
| `No space left on device` during `lake update` | The disk filled mid-download. Delete the half-made checkout (it was created by that run) before retrying. |
| `could not find platform module …` | An import names a theorem or definition the API could not resolve in that environment. |
| `HTTP 429 … too many pending` | Wait for queued submissions or publish jobs to finish instead of resubmitting. |
| `no Prove2Me API key` inside a script run | The session uses a per-environment checkout; rerun `setup` so it links the key there. |
| Edits refused as "a sensitive file" | The statements workspace is inside this checkout (the plugin directory); move it out. |
| `propose`: `… already exists and is not a local draft` | A mirrored platform module has that name in this environment; rename the draft item. |
| `propose`: `can no longer be edited` | The proposal was submitted; after a moderator sends it back (`Changes requested`) it is editable again. |
| `no visible Prove2Me mission matches` | The name differs from the URL slug, or the mission is private to someone else. |
