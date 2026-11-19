- **The verify workflow fits a large project on a hosted runner.** The
  generated `autoform-verify.yml` sets `LEAN_NUM_THREADS: "2"` for Build Lean,
  so Lake compiles two modules at a time and each `lean` runs two threads. It
  adds 16 GB of swap as a margin, and gives the job 330 minutes instead of 120.
  With four compiles at once, a large project ran the 16 GB hosted runner out
  of memory, and the runner shut down mid-build. On a small project the cap
  costs about a second. A project that fit in memory before may build slower,
  and can raise the value (#812).
