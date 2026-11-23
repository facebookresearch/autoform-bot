## Your task: make your change pass the harness's checks

You are finishing a change in this checkout for this task:

{{task_block}}

The harness re-ran its own checks and these failed:

{{findings}}

Fix the cause without weakening any statement, test or check and without unrelated changes. A
statement-fidelity failure means a task declaration, or a definition its statement uses, is not
term for term the base's version: restore the exact statement and definitions and prove that. If
the task cannot be done faithfully, report `"status": "blocked"` with the precise reason.

Re-run what failed. Then write `{{report_path}}` again, with the same keys as before (`status`,
`pr_title`, `pr_body`, `commit_message`, `declarations`, `notes`), keeping or updating the public text.
