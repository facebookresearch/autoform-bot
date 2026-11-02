<h1 align="center"><img src="assets/autoform-small.svg" width="88" alt=""><br>AutoformBot</h1>

<p align="center">
  <strong>Turn a paper or your notes into a Lean 4 formalization you can check, from your coding assistant.</strong><br>
  A plugin for Claude Code, Muse Code and Codex.
</p>

<p align="center">
  <a href="#what-you-get">What you get</a> · <a href="#installation">Installation</a> ·
  <a href="#quick-start">Quick start</a> · <a href="skills/setup/assets/cabannes-thesis-project/README.md">Example</a>
</p>

## What you get

Point AutoformBot at a paper, a book chapter or your notes. It plans the formalization, has agents prove it in Lean,
and shows exactly what is proved, as in the [example project](skills/setup/assets/cabannes-thesis-project/README.md).

```mermaid
flowchart LR
  source(["Paper or notes"]) --> setup["<b>setup</b><br>Lean project and blueprint"]
  setup --> roadmap["<b>roadmap</b><br>articles and dependencies"]
  roadmap --> formalize["<b>formalize</b><br>Lean definitions and proofs"]
  roadmap & formalize -.-> review["<b>human-review</b> · <b>agent-review</b><br>inspect the plan or the proofs"]
  roadmap & formalize --> site[("Blueprint site<br>reader and dependency map")]
```

- **A plan tied to the source.** The roadmap is a book of Markdown articles, one per definition and result, each
  citing the passage it comes from and listing what it depends on. Those dependencies split the work into
  pull-request-sized steps, and a coverage page records what is in scope and what is left out.
- **Proofs the Lean kernel has checked.** Agents take every step whose dependencies are done, several at once, and
  search Mathlib and your libraries before writing a new lemma. A step counts only once it builds without `sorry` or
  new axioms and an independent review agent finds its Lean statement faithful to the source.
- **Progress that cannot drift.** Each result's status, from planned to fully proved, is derived from the dependency
  graph on every run. The CI that setup writes builds the project on every pull request, replays each declaration
  through the Lean kernel, and fails on a nonstandard axiom or any `sorry` outside statements you chose to leave open.
- **A site to read and share.** The blueprint publishes as a mathematical book beside its dependency map, with
  search, progress figures, and a link from each statement to its Lean source, ready for GitHub Pages.
- **Statement review without reading Lean.** Lean checks proofs, not whether a theorem says what the paper says.
  Turn on statement review and each formal statement gets a blind English read-back, written by an agent that sees
  only the Lean, to compare with the source passage; your approvals are recorded statement by statement.
- **Everything in your repository.** The blueprint is Markdown in Git beside the Lean code; no service holds it.

## Installation

Requires Python 3.10+, [`uv`](https://docs.astral.sh/uv/), Git, and Lean 4 v4.27.0 or newer with Lake.

**Claude Code**

```bash
claude plugin marketplace add facebookresearch/autoform-bot@v0.6.0
claude plugin install autoform@autoform
```

**Muse Code**

```bash
muse plugins marketplace add autoform 'https://github.com/facebookresearch/autoform-bot.git#v0.6.0'
muse plugins install autoform@autoform
```

**Codex**

```bash
codex plugin marketplace add facebookresearch/autoform-bot --ref v0.6.0
codex plugin add autoform@autoform
```

These install release 0.6.0. Then start a new session in your project's repository.

<details>
<summary>Older Claude Code, moving onto the release, or following <code>main</code></summary>

- Claude Code before 2.1.71 takes `'facebookresearch/autoform-bot#v0.6.0'` instead of the `@v0.6.0` form, quoted
  for shells that read `#`.
- To move an install that follows `main` onto the release, remove the `autoform` marketplace first (`claude plugin
  marketplace remove autoform`, `muse plugins remove autoform` and then `muse plugins marketplace remove autoform`,
  or `codex plugin marketplace remove autoform`), then run the commands again.
- To follow `main` instead, drop the `@v0.6.0` or `#v0.6.0` suffix, and give Codex `--ref main`.

</details>

## Quick start

Run these skills from your agent window; there is no CLI to learn.

| Step | Claude Code | Muse Code | Codex | What it does |
| :-: | --- | --- | --- | --- |
| 1 | `/autoform:setup` | `/autoform:setup` | `$autoform:setup` | Set up the project |
| 2 | `/autoform:roadmap` | `/autoform:roadmap` | `$autoform:roadmap` | Plan from a paper or notes |
| 3 | `/autoform:formalize` | `/autoform:formalize` | `$autoform:formalize` | Write Lean definitions and proofs |
| Any time | `/autoform:human-review` | `/autoform:human-review` | `$autoform:human-review` | Review the roadmap and progress |
| Any time | `/autoform:agent-review` | `/autoform:agent-review` | `$autoform:agent-review` | Request an independent AI review |

For example, invoke `roadmap` and ask: "Build a complete roadmap for Sections 2–4 of `paper.pdf`." Keep the source
in your project, or give a path the agent can read.

Setup changes only local files unless you ask for more: a remote, a push, GitHub Pages and publishing each need an
explicit request, and a published blueprint is public. After each setup, roadmap or formalize run, a debrief notes
friction and may draft an issue for you to send, usually in `.git/autoform/`; `AUTOFORM_DEBRIEF=0` turns it off.

## Run several sessions

To put more agents on a large project than one session allows, run several sessions on it at once (in Codex, first
raise `max_threads` under `[agents]` in `~/.codex/config.toml`). The sessions coordinate through claims: temporary
leases on roadmap articles, stored as Git refs on the project's `origin`.

1. Give each session its own clone of the project, with the same `origin` and the same branch, not a worktree.
2. Tell each session that other sessions share the project, and allow it to push claims and verified results to
   `origin`, even when they start together: each one sees an empty claim board and would otherwise work without
   claims. For example, invoke `formalize` and ask: "Formalize the ready frontier. Four other sessions are working on
   this project; use claims, and push claims and verified results to `origin`."

Sessions on one machine share its memory, so start no more than it can build Lean for. They also share one Lean
runtime, whose limits [`servers/README.md`](servers/README.md) explains.

## Development

Clone with `git clone https://github.com/facebookresearch/autoform-bot.git`, then follow
[CONTRIBUTING.md](CONTRIBUTING.md). The [blueprint format and CLI reference](autoform_cli/README.md),
[`servers/README.md`](servers/README.md) and the [changelog](CHANGELOG.md) cover the rest. MIT [license](LICENSE).
