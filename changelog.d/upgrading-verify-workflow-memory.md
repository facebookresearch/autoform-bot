- **Verify workflow memory.** A project set up before this release keeps its
  `.github/workflows/autoform-verify.yml` as it was. If its Build Lean stops
  with exit code 143 ("The runner has received a shutdown signal"), or the job
  stops at its 120-minute limit, copy three settings from the template:
  `timeout-minutes: 330` on the build job, the `Add swap` step before Build
  Lean, and `LEAN_NUM_THREADS: "2"` in Build Lean's `env`. They don't depend on
  `AUTOFORM_REF`, so the pin can stay where it is (#812).
