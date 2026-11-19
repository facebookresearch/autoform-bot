- **CI audit.** A project set up before this release keeps its
  `.github/autoform_audit.py`, whose audit misses a `sorry` copy of a theorem
  when a proved copy loads after it (see **CI audit: a `sorry` copy behind a
  proved one**). Delete that file and rerun `autoform init`, which writes only
  missing files, then rerun the verify workflow. A declaration that passed only
  through the gap now fails. A verify workflow written before 0.6.0 runs the
  probe with `lake env lean`, which the probe has refused since 0.6.0: delete
  `.github/workflows/autoform-verify.yml` too before rerunning `init` (#831).
