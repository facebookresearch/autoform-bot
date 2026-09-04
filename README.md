# AutoformBot

AutoformBot is a coding-agent plugin and Python CLI for Lean 4 formalization
projects. It builds source-grounded Markdown roadmaps, validates dependencies,
publishes progress views, and prepares human or agent review. The plugin and
CLI use the identifier `autoform`; the canonical repository is
[`facebookresearch/autoform-bot`](https://github.com/facebookresearch/autoform-bot).

The default `main` branch provides repository setup, roadmap planning,
publication, human and agent review, and shared Lean LSP/REPL tools. It does
**not** include autonomous orchestration.

Autonomous execution is an opt-in overlay on the
[`execution`](https://github.com/facebookresearch/autoform-bot/tree/execution)
branch. It adds orchestration, claim-backed workers, specialist agents, and
prover adapters on top of `main`. Use `main` unless you are explicitly
evaluating that execution stack.

## Prerequisites

- Python 3.10 or newer and [`uv`](https://docs.astral.sh/uv/)
- Git
- Lean and Lake for Lean tooling and verification
- Claude Code or Codex for the installation flows below

## Install

Claude Code:

```bash
claude plugin marketplace add facebookresearch/autoform-bot
claude plugin install autoform@autoform
```

Codex:

```bash
codex plugin marketplace add facebookresearch/autoform-bot --ref main
codex plugin add autoform@autoform
```

Start a new agent session so the skills and MCP servers reload. A native Muse
manifest is included, but Muse installation is not covered here.

## Quick start

Work from an existing Lean repository. First scaffold the blueprint and site
configuration from an Autoform checkout:

```bash
uv run autoform init /path/to/lean-project \
  --autoform-ref <full-commit-sha>
```

This creates `blueprint/`, `mkdocs.yml`, and `requirements-docs.txt`. GitHub
workflows are created only when Autoform has an immutable commit pin. The Setup
skill can inspect and repair this infrastructure, but its new-project Lean
bootstrap helper is not packaged on `main`; start from an existing Lean project
and use `autoform init` for the blueprint and publication files.

Next use the host skills from the Lean project:

| Goal | Claude Code | Codex |
| --- | --- | --- |
| Build a source-grounded roadmap | `/autoform:roadmap` | `$roadmap` |
| Prepare a person-led review | `/autoform:human-review` | `$human-review` |
| Run an independent agent review | `/autoform:agent-review` | `$agent-review` |
| Refresh a minimal declaration checklist | `/autoform:declaration-review` | `$declaration-review` |

For example: “Build a roadmap for Sections 2–4 of `paper.pdf`; confirm the scope
and completion criteria before writing articles.” Keep the source in the
repository or provide an accessible path. Human and agent review are
alternatives; review the roadmap before treating it as an execution plan.

## Blueprint model

```text
blueprint/
├── README.md
├── coverage/README.md
├── roadmap/
│   ├── README.md
│   └── convexity/
│       ├── README.md
│       ├── convex.md
│       └── separating-hyperplane.md
└── sources/paper.md
```

Every Markdown file below `blueprint/roadmap/` is an article. A nested
`README.md` represents its directory and contains the articles below it.
Optional `declaration: theorem`, `declaration: def`, and similar frontmatter
marks a formalizable article. Inline relative links under `## Depends on` and
`## Proof depends on` define dependency edges; reference-style links do not.

Markdown is the source of truth; Mermaid graphs and MkDocs pages are derived
views. See the [blueprint format and CLI reference](autoform_cli/README.md) for
complete frontmatter, hierarchy, status, and validation rules.

## CLI and publication

| Command | Purpose |
| --- | --- |
| `autoform init` | Scaffold the blueprint and site; add CI when immutably pinned. |
| `autoform check` | Validate Markdown structure and dependencies. |
| `autoform audit` | Audit completeness and checked facts. |
| `autoform doctor` | Diagnose the local blueprint contract. |
| `autoform claim` | Coordinate temporary ownership through Git refs. |
| `autoform render` | Generate publishable MkDocs source. |
| `autoform-visualize` | Generate the Mermaid dependency graph. |

Inside an Autoform checkout, use `uv run`:

```bash
uv run autoform check /path/to/project/blueprint --lean-root /path/to/project
uv run autoform-visualize /path/to/project/blueprint
uv run autoform render /path/to/project/blueprint \
  --output /path/to/project/site-src \
  --lean-root /path/to/project --require-declarations
```

From a consumer project, resolve the installed plugin root and prefix commands
with `uv run --project "<AUTOFORM_PLUGIN_ROOT>"`, or separately install the
Python package so its console scripts are on `PATH`.

`check --lean-root` lexically resolves names in local Lean files; it does not
compile them or prove that they belong to a Lake target. Use `lake build` and
the verification workflow for compilation and audit, while treating the
blueprint-to-declaration match as a separate contract.

`render` writes MkDocs source, not a deployed site. The generated Pages workflow
deploys from `main` only after GitHub Pages is enabled in repository settings.

## Documentation

- [Cabannes thesis example](skills/setup/assets/cabannes-thesis-project/README.md)
- [Roadmap example](skills/roadmap/references/cabannes-thesis-roadmap.md)
- [Lean server architecture and operations](servers/README.md)

## Development

Development also requires Make:

```bash
git clone https://github.com/facebookresearch/autoform-bot.git
cd autoform-bot
make setup
make lint
make test
make check-example
```

Claude Code uses `/autoform:develop-plugin`; Codex uses `$develop-plugin`.
`make check-example` validates, renders, and builds the example documentation.
Run `lake build` in the Cabannes fixture when changing its Lean sources or
declarations.

AutoformBot is released under the [MIT License](LICENSE).
