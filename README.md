# AutoformBot

AutoformBot is a Claude Code and Codex plugin for turning mathematical sources
into a Lean 4 formalization and a readable companion site. It provides:

- repository setup for Lean, Mathlib, CI, and GitHub Pages;
- source-grounded Markdown roadmaps with explicit theorem dependencies;
- exhaustive source-unit coverage checks;
- shared Lean LSP and REPL tools;
- human and independent agent review workflows; and
- CLI-backed work discovery and durable claims for concurrent contributors.

The plugin and Python commands use the name `autoform`. The canonical repository
is [`facebookresearch/autoform-bot`](https://github.com/facebookresearch/autoform-bot).

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Git
- Lean and Lake for proof checking
- Claude Code or Codex

## Install

Claude Code:

```bash
claude plugin marketplace add facebookresearch/autoform-bot
claude plugin install autoform@autoform
```

Codex:

```bash
codex plugin marketplace add facebookresearch/autoform-bot --ref main
codex
/plugins
```

Select Autoform in the plugin browser and install it. Start a new agent session
after installation so the skills and MCP servers are loaded. A Muse manifest is
bundled, but Muse installation is separate.

## Use the plugin

Invoke the skill that matches the current stage:

| Task | Claude Code | Codex |
| --- | --- | --- |
| Set up or repair a repository | `/autoform:setup` | `$autoform:setup` |
| Build or refine the roadmap | `/autoform:roadmap` | `$autoform:roadmap` |
| Inspect the rendered plan yourself | `/autoform:human-review` | `$autoform:human-review` |
| Run an independent audit | `/autoform:agent-review` | `$autoform:agent-review` |
| Work through ready nodes | `/autoform:orchestrate` | `$autoform:orchestrate` |

A typical request is:

> Use Autoform to set up this Lean repository, build a roadmap for every result
> in `sources/book.pdf`, have an independent agent audit it, then formalize the
> ready nodes and maintain the readable companion.

Autoform keeps authored state in an Obsidian-compatible blueprint vault. Each
roadmap article records its sources, statement dependencies, proof dependencies,
and verified Lean declarations. Status, graphs, and publication pages are
derived from those files.

The skills invoke the bundled Python tools for you. Plugin installation does
not add their console scripts to the shell `PATH`. For manual use, first obtain
the absolute root of the loaded Autoform plugin, then run every command through
that project:

```bash
export AUTOFORM_PLUGIN_ROOT="<absolute-installed-plugin-root>"
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform --help
```

## Create a project

For a new Lean repository, let the Setup skill select the bundled compatible
Lean and Mathlib release. The underlying commands are:

```bash
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform project provenance --json
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform project versions --json
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform project new MyProject --package MyProject \
  --release <release-id> \
  --autoform-source https://github.com/facebookresearch/autoform-bot.git \
  --autoform-ref <full-commit-sha>
```

For an existing repository that needs several independent formalization
projects, create a workspace and register each blueprint:

```bash
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform workspace init . \
  --blueprint-root docs/blueprints
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform blueprint new textbook \
  --path Textbook --title "Textbook"
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform workspace inspect .
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform workspace check . --lean-root .
```

The root `.autoform.toml` is the only workspace registry. Registered blueprint
paths must not overlap. The original single-project layout remains available:

```bash
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform init . \
  --autoform-source https://github.com/facebookresearch/autoform-bot.git \
  --autoform-ref <full-commit-sha>
```

## Validate and publish

Run these commands from a Lean project that uses the original single-project
layout:

```bash
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform doctor . --lean-root .
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform check blueprint --lean-root .
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform audit blueprint --lean-root .
lake build
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform render blueprint --output site-src \
  --lean-root . --require-declarations
```

In a registered multi-project workspace, use `.` instead of `blueprint` and add
`--project <id>` to each `autoform` command.

`check` validates the Markdown dependency graph. `audit` checks roadmap and
source coverage. `lake build` checks Lean. `render` produces MkDocs source for
the human-readable companion; the generated Pages workflow can publish it once
GitHub Pages is enabled.

For the complete blueprint format and command flags, see the
[CLI reference](autoform_cli/README.md).

## Formalize ready work

The Orchestrate skill uses the public CLI rather than a separate worker
runtime. First list the statement or proof phases whose roadmap prerequisites
are satisfied. This command also requires a complete source-unit coverage
contract:

```bash
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform ready . --lean-root . --json
```

Before editing an item, acquire its returned `article_id`. Each concurrent
contributor uses a separate Git worktree and a fail-closed Git-ref claim:

```bash
export AUTOFORM_WORKER_ID="worker-name"
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform claim acquire <article-id>
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform claim renew <article-id>
uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform claim release <article-id>
```

Use the `claim list` subcommand to inspect ownership. Contributors also claim shared
resources such as `lake-build` before using a shared build cache. The current
host agent performs the Lean work with the bundled LSP and REPL, runs the
focused Lake build, obtains an independent source-faithfulness and proof review,
records the exact verified metadata, and then runs `autoform check` and
`autoform audit` against that final state. Release the
article claim only after the verified commit reaches the authorized shared
branch. If an attempt is abandoned without a candidate, release it. For an
integration failure or handoff, report the branch, commit, and claim state
instead of making unfinished work appear available. Then call `autoform ready`
again from the updated shared base.
In a registered workspace, pass the same `--project <id>` to the ready, claim,
check, and audit commands.

## Development

```bash
git clone https://github.com/facebookresearch/autoform-bot.git
cd autoform-bot
make setup
make lint
make test
make check-example
```

Plugin maintainers can use `/autoform:develop-plugin` in Claude Code or
`$autoform:develop-plugin` in Codex. AutoformBot is released under the
[MIT License](LICENSE).
