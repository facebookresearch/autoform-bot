## Your task: one ready article

{{task_block}}

Work this one article's `{{phase}}` phase, end to end, in this checkout. It is detached at the base
branch's tip, `{{base}}`, with the Lean packages and the base build in place: build only what you
change and what imports it.

Follow Autoform's one-article procedure, `{{plugin_root}}/skills/formalize/references/leaf.md`, as
the worker of a run whose lead is the harness, with these changes:

- The harness already holds the article's claim and prepared this checkout: skip creating a branch,
  copying packages and acquiring claims, and run no Lake command outside this checkout.
- An independent review happens on the pull request the harness opens: do not wait for one.
- Do not integrate, land, push or debrief: stop after your checks pass and write the report.
- Where the repository's rules or the project instructions above say how this repository records a
  result, follow them instead of the procedure.

Stop instead of proving something else:

- If the statement is false, vacuous, or does not say what its article and source say, change
  nothing that weakens it: report `"status": "blocked"` with the evidence (a counterexample, the
  precise defect).
- If after a serious attempt the proof needs a large theory nobody has formalized, report
  `"status": "blocked"` with what is missing.

`blocked` means only one of those two. A proof that is long, slow or costly is not blocked: write
it. Aim to finish within {{minutes}} minutes; the harness ends the attempt if time runs out.

Before you finish, check your result the way the harness will: `lake build` of every module you
added or changed and of every module that imports one you changed or deleted; `#print axioms` of
each task declaration (only `propext`, `Classical.choice` and `Quot.sound`); the statement of each
task declaration unchanged, binders, hypotheses, universes and conclusion, term for term, and every
definition it uses unchanged{{verify_hint}}

## Report

Write `{{report_path}}` (it is outside the checkout, so it is never committed) as one JSON object:

- `status`: `"done"` or `"blocked"`;
- `pr_title`: an imperative title under 72 characters, such as "Prove the X theorem";
- `pr_body`: the pull-request description, public text. {{template_hint}} Say what is proved, where
  it now lives, what moved or was deleted, which source it follows and which tests you added. Name
  no declaration of another task;
- `commit_message`: one line, imperative;
- `declarations`: the task declarations' full names;
- `notes`: anything a reviewer should know, and for `"blocked"` the evidence.
