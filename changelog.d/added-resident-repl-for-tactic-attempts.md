- **Resident REPL for tactic attempts.** With `AUTOFORM_TACTIC_BACKEND=repl`,
  `lean_try_tactics` and `lean_try_close` run in one long-lived REPL per project
  that imports a file's header once, project modules included, and tries each
  candidate at the goal a `sorry` leaves. One that closes that goal is `closed`
  only once the declaration elaborates with it written in. Anything the REPL
  cannot serve falls back to the language server, and says so. Lean's header
  parser checks each header, a rebuild of any project module below it restarts
  the REPL, and an import changed since its last build is named in the result.
  `AUTOFORM_RESIDENT_REPL_MAX_GB` (default 8) bounds its memory (#805).
