# AutoformBot

A plugin for Claude Code and Codex that helps turn mathematical papers and
notes into Lean 4 formalizations: plan the work, write proofs, and review
progress from your coding assistant.

## Installation

Requires Python 3.10+, [`uv`](https://docs.astral.sh/uv/), Git, and Lean 4
v4.27.0 or newer with Lake.

**Claude Code**

```bash
claude plugin marketplace add facebookresearch/autoform-bot@v0.6.0
claude plugin install autoform@autoform
```

**Codex**

```bash
codex plugin marketplace add facebookresearch/autoform-bot --ref v0.6.0
codex plugin add autoform@autoform
```

**Muse Code**

```bash
muse plugins marketplace add autoform 'https://github.com/facebookresearch/autoform-bot.git#v0.6.0'
muse plugins install autoform@autoform
```

These commands install release 0.6.0. Claude Code reads the `@v0.6.0` form from
version 2.1.71; older versions take `'facebookresearch/autoform-bot#v0.6.0'`,
quoted for shells that read `#`. To move an install that follows `main` onto the
release, remove the `autoform` marketplace first, with `claude plugin
marketplace remove autoform`, `codex plugin marketplace remove autoform`, or
`muse plugins remove autoform` and then `muse plugins marketplace remove
autoform`, and run the commands again. To follow `main` instead, drop the
`@v0.6.0` or `#v0.6.0` suffix, and give Codex `--ref main`.

After installing, start a new session in the repository you want to inspect or
set up.

## Quick start

Use these skills from your agent window; users do not need to learn or run its
commands:

| Task | Claude Code | Codex |
| --- | --- | --- |
| Set up the project | `/autoform:setup` | `$autoform:setup` |
| Plan from a paper or notes | `/autoform:roadmap` | `$autoform:roadmap` |
| Write Lean definitions and proofs | `/autoform:formalize` | `$autoform:formalize` |
| Review the roadmap and progress | `/autoform:human-review` | `$autoform:human-review` |
| Request an independent AI review | `/autoform:agent-review` | `$autoform:agent-review` |

Start with `setup`, then use `roadmap` to plan your formalization and
`formalize` to work through it. Use either review command to inspect the plan
or the resulting formalization.

For example, invoke `roadmap` and ask:

> Build a complete roadmap for Sections 2–4 of `paper.pdf`.

Keep the source file in your project or provide an accessible path.

Setup changes local files by default. Creating or pushing a remote repository,
enabling GitHub Pages, and publishing blueprint content require an explicit
request; content published through Pages is public.

## Run several sessions

Codex limits how many agents one session runs at once; raise `max_threads`
under `[agents]` in `~/.codex/config.toml` before reaching for more sessions.

To put more agents on a large project than one session allows, run several
sessions on it at once. They coordinate through claims: temporary leases on
roadmap articles, stored as Git refs on the project's `origin`, so no session
can take a leaf another one holds.

1. Give each session its own clone of the project, with the same `origin` and
   the same branch. Git checks out a branch in only one worktree at a time.
2. Tell each session that other sessions share the project, and allow it to
   push claims and verified results to `origin`. For example, invoke `formalize`
   and ask:

   > Formalize the ready frontier. Four other sessions are working on this
   > project; use claims, and push claims and verified results to `origin`.

Say so even when the sessions start together: each one sees an empty claim
board and would otherwise work without claims. Sessions on one machine share its
memory, so start no more than it can build Lean for. They also share one Lean
runtime, which by default keeps four checkouts loaded and runs one REPL call per
32 GB of memory; raise its limits as [`servers/README.md`](servers/README.md)
describes.

## Blueprint model

Autoform keeps the roadmap and dependency graph as Markdown under
`blueprint/`; publication graphs and pages are derived. See the
[blueprint format and CLI reference](autoform_cli/README.md) for the complete
format and command contracts, or browse the
[Cabannes thesis example](skills/setup/assets/cabannes-thesis-project/README.md).

The browser publication presents large roadmaps as a full-page mathematics
atlas: authored areas and bounded hierarchy preserve readable context, while
dependency edges remain distinct from containment and repository provenance.

## Search and reuse

Before adding a result to a roadmap, or a lemma to a Lean project, agents
search the blueprint and any shared Lean library the project depends on, such
as [ATLAS](https://github.com/facebookresearch/atlas-lean), so that a result
that already exists is reused rather than proved again. A library's search
index is used only after it has been checked against the exact library version
the project builds with.

The paper
[*Search Before You Prove: Verified Retrieval and Reuse of Shared Lean Libraries in Multi-Agent Autoformalization*](docs/papers/search-and-reuse/search-and-reuse.pdf)
explains the design and its trade-offs, and evaluates it on ATLAS's published
index.

## Development

```bash
git clone https://github.com/facebookresearch/autoform-bot.git
cd autoform-bot
make setup
make lint
make test
make check-example
make test-browser  # the dependency explorer in WebKit
```

Lean server architecture and operations are documented in
[`servers/README.md`](servers/README.md).

## License

[MIT](LICENSE).
