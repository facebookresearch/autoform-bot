## Your task: bring your pull request up to date with the base branch

The checkout holds pull request #{{number}}:

{{pr_block}}

The harness started a merge of the base branch's tip, `{{base}}`, into it, and the merge stopped
on conflicts (see `git status`). Resolve every conflict so that the pull request keeps its intent
and the base branch's changes are kept. If the base branch now already proves or removes the task's
declarations, stop and report `"status": "obsolete"`.

Stage the resolved files with `git add`, but do not commit: the harness commits the merge. Then run
`lake build` on the changed modules and the repository's quick checks.

Write `{{report_path}}` as one JSON object: `status` (`"done"`, `"blocked"` or `"obsolete"`),
`summary` (public text) and `notes`.
