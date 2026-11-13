<h1 align="center"><img src="assets/autoform-small.svg" width="88" alt=""><br>AutoformBot</h1>

<p align="center">
  <strong>Turn a paper or your notes into a Lean 4 formalization you can check.</strong><br>
  A plugin for your coding assistant: Claude Code, Muse Code or Codex.
</p>

<p align="center">
  <a href="#what-you-get">What you get</a> · <a href="#installation">Installation</a> ·
  <a href="#quick-start">Quick start</a> · <a href="skills/setup/assets/cabannes-thesis-project/README.md">Example</a>
</p>

## What you get

Point AutoformBot at a paper, a book chapter or your notes. It plans the formalization, has agents carry it out in Lean,
and shows exactly what is proved. See the [example project](skills/setup/assets/cabannes-thesis-project/README.md).

```mermaid
flowchart LR
  source(["Paper or notes"]) --> agents["<b>Your agents</b><br>plan and prove"]
  agents <--> cli{{"<b>autoform</b> CLI<br>work · search · links<br>move · check · audit"}}
  cli <--> wiki[("<b>Blueprint wiki</b><br>only verified facts")]
  agents --> lean[("Lean project")]
  cli -.->|"checks every Lean name"| lean
  wiki --> site[("<b>Blueprint site</b><br>book and dependency map")]
```

## Installation

Requires Python 3.11 to 3.14, [`uv`](https://docs.astral.sh/uv/), Git, and Lean 4 v4.27.0 or newer with Lake.

**Claude Code**

```bash
claude plugin marketplace add facebookresearch/autoform-bot@v0.7.0
claude plugin install autoform@autoform
```

**Muse Code**

```bash
muse plugins marketplace add autoform 'https://github.com/facebookresearch/autoform-bot.git#v0.7.0'
muse plugins install autoform@autoform
```

**Codex**

```bash
codex plugin marketplace add facebookresearch/autoform-bot --ref v0.7.0
codex plugin add autoform@autoform
```

These commands install release 0.7.0. Then start a new session in your project, or anywhere for a new project.

<details>
<summary>Other install options</summary>

- **On Claude Code before 2.1.71**, run `claude plugin marketplace add 'facebookresearch/autoform-bot#v0.7.0'` instead.
- **To follow `main`**, drop the `@v0.7.0` or `#v0.7.0` suffix, and give Codex `--ref main`.
- **To move from `main` or an earlier release**, remove the `autoform` marketplace, then run the commands above.
  Claude Code: `claude plugin marketplace remove autoform`. Codex: `codex plugin marketplace remove autoform`.
  Muse Code: `muse plugins remove autoform`, then `muse plugins marketplace remove autoform`.

</details>

## Quick start

Type these into your coding assistant; it runs AutoformBot's CLI for you.

| Step | Claude Code, Muse Code | Codex | What it does |
| :-: | --- | --- | --- |
| 1 | `/autoform:setup` | `$autoform:setup` | Set up the Lean project and blueprint |
| 2 | `/autoform:roadmap` | `$autoform:roadmap` | Plan from a paper or notes |
| 3 | `/autoform:formalize` | `$autoform:formalize` | Write Lean definitions and proofs |
| Any time | `/autoform:human-review` | `$autoform:human-review` | Review the roadmap, progress and statements |
| Any time | `/autoform:agent-review` | `$autoform:agent-review` | Request an independent AI review |

For example, invoke `roadmap` and ask: "Build a complete roadmap for Sections 2–4 of `paper.pdf`." Keep the source
in your project, or give a path the agent can read.

## Run agents in parallel

A `formalize` session runs one agent per ready result, as many at once as the machine can build Lean for; in Codex,
raise `max_threads` under `[agents]` in `~/.codex/config.toml` to allow more. Several sessions, on one machine or many,
share a project by claiming articles on a claim board kept as Git refs on `origin`:

1. Give each session its own clone of the project, with the same `origin` and the same branch (not a `git worktree`).
2. Tell each session that other sessions share the project, and let it push claims and verified results to
   `origin`, even when they all start together: each one sees an empty claim board and would otherwise work alone.
3. Have each session release its claims when it stops: a claim nobody releases blocks its articles for seven days.

For example, invoke `formalize` in each session and ask: "Formalize the results that are ready. Four other sessions
share this project; use claims, and push claims and verified results to `origin`."

Sessions on one machine share its memory, and each builds Lean: start only as many as fit ([limits](servers/README.md)).

## Search and reuse

Before adding a result, agents search the blueprint and every dependency that publishes a search
index Autoform can verify, such as [ATLAS](https://github.com/facebookresearch/atlas-lean).

## Development

Clone with `git clone https://github.com/facebookresearch/autoform-bot.git`, then follow
[CONTRIBUTING.md](CONTRIBUTING.md). The [blueprint format and CLI reference](autoform_cli/README.md),
[`servers/README.md`](servers/README.md) and the [changelog](CHANGELOG.md) cover the rest. MIT [license](LICENSE).

Issues and ideas are welcome. After a setup, roadmap or formalize run, your agent may end its report with a link to a
draft issue about where AutoformBot got in its way; you decide whether to send it. `AUTOFORM_DEBRIEF=0` turns this off.
