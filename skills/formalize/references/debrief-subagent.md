# Debrief a finished attempt (subagent)

Once an attempt is over and its claim released or handed off, the agent that
worked it runs `autoform debrief subagent form <ARTICLE> <PROJECT> --phase
statement|proof` from the checkout where the result is visible (see the
[CLI reference](../../../autoform_cli/README.md#commands)). Pass `--note` with
the reason when the attempt did not succeed. If the command says subagent
debriefs are disabled, stop: there is nothing to do.

Otherwise answer the form from your own experience of the attempt and store the
answer with `autoform debrief subagent record` using the same arguments plus
`--worker-id` (the same ID as on your claim commands) and `--answer FILE` or
standard input. If `record` rejects the answer, correct it and resubmit.

A subagent debriefs before reporting, because only it witnessed the searches and
dead ends; the lead must not answer on its behalf.
