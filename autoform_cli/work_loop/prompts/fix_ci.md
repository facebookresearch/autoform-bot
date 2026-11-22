## Your task: repair a failed check on your pull request

The checkout holds pull request #{{number}} at its head, `{{head}}`:

{{pr_block}}

These checks failed on this head:

{{findings}}

Find the cause from the logs and reproduce it locally when you can. Fix the change so the check
passes, without weakening a statement, a test or any check, and without unrelated changes. If the
failure does not come from this change (the base branch fails the same way, or the check is broken),
change nothing and report `"status": "blocked"` saying why.

Write `{{report_path}}` as one JSON object: `status` (`"done"` or `"blocked"`), `commit_message`
(one public line), `summary` (public text, one to three sentences) and `notes`.
