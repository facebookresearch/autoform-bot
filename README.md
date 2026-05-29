# Autoformalization Pipeline

Multi-agent system for translating LaTeX mathematics into verified Lean 4 proofs using Mathlib.

![Visualizer Dashboard](docs/assets/visualizer.png)

## Setup

```bash
make setup    # creates venv, installs deps, builds Lean + REPL (~20 min)
```

Create `.env` with the API key for your chosen provider:
```
ANTHROPIC_API_KEY=your-key-here   # for Claude models
OPENAI_API_KEY=your-key-here      # for GPT models
GEMINI_API_KEY=your-key-here      # for Gemini models
ARISTOTLE_API_KEY=arstl_...       # for Harmonic's Aristotle (see note below)
```

**Aristotle (Harmonic).** The `"Aristotle"` model routes to Harmonic's
autonomous formal-reasoning agent instead of a chat LLM. It is an optional
backend — install with `pip install -e ".[aristotle]"` (pulls in
`aristotlelib`) and mint a key at
<https://aristotle.harmonic.fun/dashboard/keys>. Because Aristotle runs its
own internal tools and returns finished Lean files rather than per-turn tool
calls, it carries integration caveats documented in
[`core/inference/sdk/aristotle.py`](core/inference/sdk/aristotle.py): no
per-turn tool calling, no in-flight steering (steer between turns via
follow-up prompts), and no token-usage/caching accounting.

## Quick Start

**1. Prepare book data** — place `book.md` (and optionally `book.pdf`) in `autoform/data/<name>/`. See `autoform/data/example/` for a sample.

**2. Extract targets:**
```bash
python -m autoform.statement_extraction run \
    --book-dir=autoform/data/my_book \
    --output=autoform/data/my_book/targets.yaml
```

**3. Create a config** (see `autoform/bot/configs/` for examples):
```yaml
workspace:
  path: ../my-workspace
  mathlib_path: submodules/mathlib
  lib_name: My_Book

book:
  path: my_book
  files: [book.md]
  targets: targets.yaml

llm:
  model: Opus 4.6

workers:
  agents_per_node: 5
  num_repls_per_node: 5
  min_agents_per_task: 3
  max_agents_per_task: 5
```

**4. Run:**
```bash
# Start fresh
python -m autoform.bot.main run --config=path/to/config.yaml --name=my-run --fresh

# Resume an interrupted run (omit --fresh)
python -m autoform.bot.main run --config=path/to/config.yaml --name=my-run

# Multi-node with SLURM
srun --nodes=N --ntasks-per-node=1 python -m autoform.bot.main run --config=... --name=my-run
```

**5. Monitor:**
```bash
python -m autoform.visualizer.app --runs-dir=../my-workspace --port=8003
```

**6. Evaluate:**
```bash
python -m autoform.eval run \
    --repo-dir=../my-workspace/my-run/code \
    --task-file=autoform/data/my_book/targets.yaml \
    --book-dir=autoform/data/my_book
```

## Architecture

```
autoform-pipeline/
├── autoform/
│   ├── bot/                  Multi-agent pipeline (orchestrator, workers, reviewers)
│   ├── eval/                 Evaluation (grading, lean checks, metrics, rubrics)
│   ├── visualizer/           Web dashboard for inspecting runs and traces
│   ├── statement_extraction/ Statement chunking and extraction from LaTeX
│   └── data/                 Book datasets (book.md + targets.yaml)
├── core/                     Framework (agent, inference, trace, coordination)
├── tools/                    MCP tool servers (filesystem, git, bash, Lean REPL/LSP, mathlib)
├── template/                 Lean 4 + Mathlib workspace template
├── submodules/               Git submodules (mathlib, repl, lean-lsp-mcp)
└── docs/                     Documentation
```

## Documentation

**Pipeline:**
- [Bot](docs/pipeline/bot.md) — multi-agent architecture, DAG workflow, multi-node SLURM, agent roles, config reference
- [Evaluation](docs/pipeline/eval.md) — matching, axiom checking, LLM grading rubrics, dependency graphs
- [Statement Extraction](docs/pipeline/statement_extraction.md) — chunking, multi-agent extraction, deduplication
- [Visualizer](docs/pipeline/visualizer.md) — dashboard views, API endpoints, hub mode

**Tools:**
- [Tools Overview](docs/tools/overview.md) — MCP tool system, available servers, adding new tools
- [REPL Reference](docs/tools/repl.md) — Lean REPL architecture, pooled server, Python API

## License

This project is licensed under the [CC BY-NC 4.0](LICENSE) license.
