- **An unattended work loop.** `autoform work run --loop` (or `work --loop`)
  keeps contributing to a project (#870). Each round maintains one of
  the loop's own pull requests (a person's comment, a conflict, the review
  bot's requested changes, a failed check) or authors a new task: it claims a
  random free task on the claim board, runs Claude Code or Codex without GitHub
  credentials, checks the result itself (proof holes, the build, each target's
  statement against the base's, the profile's commands), and opens a ready pull
  request from your fork that names the task. It follows the pull request until
  a maintainer could merge it, then releases the claim; it never merges.
  `--workers`, `--budget`, `--max-attempts`, `--max-open-prs`, `--max-hours`
  and a `STOP` file bound it, and `autoform work status` reports it. A profile
  (`.autoform/work.toml`) gives a project's task source, checks and review bot;
  facebookresearch/atlas-lean's ships with AutoformBot. The README shows how to
  launch the loop from an AutoformBot checkout.
