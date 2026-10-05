# AutoformBot

A plugin for Claude Code and Codex that helps turn mathematical papers and
notes into Lean 4 formalizations: plan the work, write proofs, and review
progress from your coding assistant.

## Installation

Requires Python 3.10+, [`uv`](https://docs.astral.sh/uv/), Git, and Lean 4
with Lake.

**Claude Code**

```bash
claude plugin marketplace add facebookresearch/autoform-bot
claude plugin install autoform@autoform
```

**Codex**

```bash
codex plugin marketplace add facebookresearch/autoform-bot --ref main
codex plugin add autoform@autoform
```

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

## Blueprint model

Autoform keeps the roadmap and dependency graph as Markdown under
`blueprint/`; publication graphs and pages are derived. See the
[blueprint format and CLI reference](autoform_cli/README.md) for the complete
format and command contracts, or browse the
[Cabannes thesis example](skills/setup/assets/cabannes-thesis-project/README.md).

## Development

```bash
git clone https://github.com/facebookresearch/autoform-bot.git
cd autoform-bot
make setup
make lint
make test
make check-example
```

Lean server architecture and operations are documented in
[`servers/README.md`](servers/README.md).

## License

[MIT](LICENSE).
